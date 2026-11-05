"""Install the single ready Lite package; never reconcile an existing Workflow."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys

from .graph import graph_matches_spec, native_graph_postimage, plan_workflow_graph
from .kent import KentClient, KentCommandError
from .model import EdgeSpec, NodeSpec, ParameterSpec, SpecError, WorkflowSpec
from . import operations as op

KIT = Path(__file__).resolve().parents[1]
TEMPLATE = "engineering-lite-v1"
SCHEMA = "ready-lite-install-v1"
PACKAGE = "templates/workflows/engineering-lite-v1.spec.json"
KIT_FILES = (
    PACKAGE, "workflowkit/lite_install.py", "scripts/install",
    "tests/test_lite_install.py", "prompts/project-bootstrap.md", "docs/OPERATIONS.md",
)
# The -I installer enters this package through its real initializer. Bind the
# exact imported local execution closure, not unrelated consumer/runtime files.
DEPENDENCY_FILES = tuple("workflowkit/" + name for name in (
    "__init__.py", "delivery.py", "graph.py", "kent.py", "model.py",
    "profile.py", "release.py", "revision.py", "runtime.py", "operations.py",
))
PROJECT_FILES = (
    "AGENTS.md", ".kent/workflow-profile.toml", ".kent/project-contract.md",
    ".kent/context/engineering-lite.md", ".kent/commands/delivery-lite.md",
    ".kent/commands/cleanup-lite.md",
)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise SpecError(message)


def file_hashes(root, names):
    result = {}
    for name in names:
        filename = root / name
        require(filename.is_file() and not filename.is_symlink(), f"regular file required: {filename}")
        result[name] = digest(filename.read_bytes())
    return result


def load_spec():
    value = json.loads((KIT / PACKAGE).read_bytes())
    nodes = tuple(NodeSpec(**row) for row in value["nodes"])
    edges = []
    for row in value["edges"]:
        row = dict(row)
        row["parameters"] = tuple(ParameterSpec(**item) for item in row.get("parameters", []))
        edges.append(EdgeSpec(**row))
    spec = WorkflowSpec(
        name=value["name"], description=value["description"],
        execution_target=value["execution_target"], nodes=nodes, edges=tuple(edges),
    )
    spec.validate()
    require(len(nodes) == 11 and all(n.kind in {"start", "agent", "terminal"} for n in nodes),
            "ready Lite must retain its fixed eleven-node Agent topology")
    return spec


def qualified_identity(root, client):
    """Reuse the existing positive native identity contract; Git failure is fatal."""
    op._source_canonical_root(str(root), "Lite primary checkout")
    require(client.workspace == client.project_workspace == root, "primary checkout differs")
    top = op._git(root, "rev-parse", "--show-toplevel").strip()
    require(top == str(root), "Git checkout root differs")
    worktrees = op._git(root, "worktree", "list", "--porcelain").splitlines()
    require(bool(worktrees) and worktrees[0] == f"worktree {root}", "Git primary checkout differs")
    project_id = client.run(["project", str(root)]).stdout.strip()
    require(project_id.startswith("project-"), "native project identity unavailable")
    workspace_id = op._source_native_identity(
        project_id=project_id, project_root=root, primary_root=root,
        kent_path=Path(client.binary), source_only=True,
    )
    return project_id, workspace_id


def observe(root, client):
    """Read-only state binding. No storage initialization or Task operations."""
    project_id, workspace_id = qualified_identity(root, client)
    workspaces = client.run_json(
        ["project", "workspaces", "--project", project_id, "--limit", "100", "--json"]
    )
    require(workspaces.get("next_offset") is None, "workspace inventory exceeds this installer")
    require(workspaces.get("project_id") == project_id
            and workspaces.get("offset") == 0, "native workspace scope differs")
    primary = [row for row in workspaces.get("workspaces", []) if row.get("is_default")]
    require(len(primary) == 1 and primary[0].get("workspace_id") == workspace_id
            and primary[0].get("root_path") == str(root), "native primary workspace drift")
    return {
        "project_id": project_id,
        "primary_workspace_id": workspace_id,
        "workspaces": workspaces,
        "linked_workflows": list(client.workflow_records(project_id=project_id)),
        "workflow_inventory": list(client.workflow_records()),
        "project_files": file_hashes(root, PROJECT_FILES),
        "kit_files": file_hashes(KIT, KIT_FILES),
        "execution_dependencies": file_hashes(KIT, DEPENDENCY_FILES),
        "kent_executable": str(Path(client.binary).resolve()),
        "kent_sha256": digest(Path(client.binary).read_bytes()),
    }


def project_spec(root, client):
    # This cached Git hint is not authority; observe positively qualifies both roots.
    require(client.project_workspace == root, "run from the primary project checkout")
    spec = load_spec()
    project_id = client.run(["project", str(root)]).stdout.strip()
    require(project_id.startswith("project-"), "native project identity unavailable")
    return replace(spec, name=f"Engineering Lite v1 [{project_id}]")


def preview(root, client):
    client.require_version(2, 10, 0)
    spec = project_spec(root, client)
    state = observe(root, client)
    require(not any(row.get("name") == spec.name for row in state["workflow_inventory"]),
            "ready Lite name already exists; inspect it, never update by name")
    empty = {"workflow_id": "00000000-0000-4000-8000-000000000000", "expected_version": 0,
             "graph": {"nodes": [], "edges": [], "node_groups": [], "transition_groups": []}}
    graph = plan_workflow_graph(spec, empty, metadata_changed=False).document["graph"]
    return {
        "schema": SCHEMA, "template": TEMPLATE, "project_root": str(root),
        "name": spec.name, "description": spec.description,
        "execution_target": spec.execution_target, "state": state,
        "graph": native_graph_postimage(graph, workflow_id=empty["workflow_id"]),
        "effects": ["create one Workflow", "apply exact complete graph",
                    "set new Workflow execution target", "validate", "link non-default"],
    }


def apply(plan, raw, confirmation, client):
    require(digest(raw) == confirmation, "exact preview SHA-256 confirmation required")
    require(raw == encode(plan), "preview bytes must be canonical")
    require(plan.get("schema") == SCHEMA and plan.get("template") == TEMPLATE, "unsupported plan")
    root = Path(plan["project_root"]).resolve(strict=True)
    require(root == client.workspace == client.project_workspace, "project root differs")
    client.require_version(2, 10, 0)
    spec = project_spec(root, client)
    require(plan["state"] == observe(root, client), "source or native state drift; obtain fresh preview")
    require(plan["name"] == spec.name and plan["description"] == spec.description
            and plan["execution_target"] == spec.execution_target, "metadata differs")
    require(graph_matches_spec(spec, {"workflow_id": "00000000-0000-4000-8000-000000000000",
                                     "expected_version": 0, "graph": plan["graph"]}),
            "preview graph differs from the ready package")
    require(not any(row.get("name") == spec.name for row in plan["state"]["workflow_inventory"]),
            "refusing existing Workflow")
    # This is deliberately create-only. Never call KentClient.apply's name reconciliation.
    client.run_json(["workflow", "create", "--description", spec.description, "--json", spec.name])
    # Native names are not ownership. Identify our sole new row before touching a graph.
    before = {row["id"] for row in plan["state"]["workflow_inventory"]}
    added = [row for row in client.workflow_records() if row["id"] not in before]
    require(len(added) == 1 and added[0].get("name") == spec.name,
            "creation inventory is ambiguous; preserve effects and inspect, never reconcile by name")
    workflow_id = added[0]["id"]
    try:
        definition = client.require_inspect(workflow_id)
        inspected = client.inspect_graph(workflow_id)
        require(all(not inspected["graph"][key] for key in (
            "nodes", "edges", "node_groups", "transition_groups")), "new Workflow is not empty")
        document = dict(inspected, graph=plan["graph"])
        client.apply_graph_document(document, confirm_destructive=False)
        client.ensure_workflow_metadata(workflow_id, spec, definition)
        after = client.inspect_graph(workflow_id)
        require(after["graph"] == native_graph_postimage(plan["graph"], workflow_id=workflow_id),
                "exact graph readback differs")
        client.assert_exact_graph_document(spec, after)
        client.validate(workflow_id)
        client.link(workflow_id, set_default=False)
        client.validate(workflow_id)
        result = client.require_inspect(workflow_id)
        links = client.workflow_records(project_id=plan["state"]["project_id"])
        added = [row for row in links if row.get("id") == workflow_id]
        require(len(added) == 1 and added[0].get("project_link", {}).get("default") is False,
                "new non-default project link not proven")
        require([row for row in links if row.get("id") != workflow_id]
                == plan["state"]["linked_workflows"], "existing project links changed")
        return result
    except Exception as error:
        raise KentCommandError(
            f"new Workflow {workflow_id} may be partially installed; preserve and settle effects; "
            f"do not retry or delete automatically: {error}"
        ) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("preview")
    read.add_argument("--project", type=Path, required=True)
    read.add_argument("--template", choices=[TEMPLATE], required=True)
    write = commands.add_parser("apply")
    write.add_argument("--plan", type=Path, required=True)
    write.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        executable = shutil.which("kent")
        require(executable is not None, "Kent executable unavailable")
        if args.command == "preview":
            root = args.project.expanduser().resolve(strict=True)
            value = preview(root, KentClient(root, binary=executable))
        else:
            raw = args.plan.read_bytes()
            require(len(raw) <= 4 * 1024 * 1024, "preview exceeds 4 MiB")
            plan = json.loads(raw)
            root = Path(plan["project_root"]).resolve(strict=True)
            value = apply(plan, raw, args.confirm, KentClient(root, binary=executable))
        sys.stdout.buffer.write(encode(value))
        return 0
    except (OSError, ValueError, KeyError, KentCommandError, op.OperationError) as error:
        print(f"install workflow: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
