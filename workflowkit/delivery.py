from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
import unicodedata
import uuid

from .model import EdgeSpec, NodeSpec, ParameterSpec, SpecError, WorkflowSpec
from .profile import ProjectProfile


PACKET_LIMIT = 1024 * 1024
PACKET_FOOTER = "KENT_PACKET_SOURCE_V1 "
_PACKET_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PACKET_LOCATOR = re.compile(
    r"build/kent-workflow/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/"
    r"packets/base-([0-9a-f]{64})\.json\Z"
)
_IDENTITY_KEYS = {
    "task_id", "task_short_id", "project_id", "workspace_path",
    "session_id", "step_id",
}
_LEGACY_IDENTITY_KEYS = _IDENTITY_KEYS | {"run_id"}
_SCRIPT_IDENTITY_KEYS = {
    "task_id", "task_short_id", "project_id", "workspace_path",
    "invocation_kind", "node_id", "transition_branch_key",
}
_PR_TARGET_CARRIER_FIELDS = {
    "pr_url",
    "branch_name",
    "pr_report",
    "merge_report",
    "pr_feedback_cursor",
    "pr_head_oid",
    "pr_base_oid",
    "ci_contract",
    "ci_report",
    "publication_report",
}
_CONTROL_KEYS = {
    "task", "task_id", "project_id", "workspace",
    "session", "session_id", "run", "run_id", "step", "step_id",
    "actor", "force", "target_node", "target", "node_id",
    "invocation_kind", "transition_branch_key",
}

SOURCE_ADMISSION_BEGIN = "KENT_SOURCE_ADMISSION_V1_BEGIN"
SOURCE_ADMISSION_END = "KENT_SOURCE_ADMISSION_V1_END"
UNACTIVATED_SOURCE = """UNACTIVATED SOURCE CANDIDATE.
Do not begin Task work or run project-local helpers from this candidate.
Final native rendering requires qualified published sources, the installed
binding, and an approved update with verified compatibility and a quiet window.
A source snapshot or matching Workflow UUID is not activation proof."""


_OBSERVED_SCRIPT_DELIVERY_TOKEN = object()


class ObservedScriptDeliveryGraph:
    """Fresh native graph obtained from the current Script's native context."""

    __slots__ = (
        "_identity_raw", "_task_detail_raw", "_graph_raw", "profile", "_token",
    )

    def __init__(self, *, identity, task_detail, graph, profile, _token):
        if _token is not _OBSERVED_SCRIPT_DELIVERY_TOKEN:
            raise TypeError("ObservedScriptDeliveryGraph is created by native observation")
        self._identity_raw = packet_json(identity)
        self._task_detail_raw = packet_json(task_detail)
        self._graph_raw = packet_json(graph)
        self.profile = profile
        self._token = _token

    @property
    def identity(self):
        return packet_decode(self._identity_raw)

    @property
    def task_detail(self):
        return packet_decode(self._task_detail_raw)

    @property
    def graph(self):
        return packet_decode(self._graph_raw)


def _observed_delivery_values(
    observation: ObservedScriptDeliveryGraph,
) -> tuple[ProjectProfile, dict, dict, dict]:
    if (
        not isinstance(observation, ObservedScriptDeliveryGraph)
        or observation._token is not _OBSERVED_SCRIPT_DELIVERY_TOKEN
    ):
        raise SpecError("delivery protocol selection requires a fresh native Script observation")
    return (
        observation.profile,
        observation.identity,
        observation.task_detail,
        observation.graph,
    )


def _native_delivery_transitions(graph: dict) -> list[dict]:
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("workflow_id"), str)
        or not isinstance(graph.get("graph"), dict)
    ):
        raise SpecError("native delivery graph identity is invalid")
    definition = graph["graph"]
    nodes = definition.get("nodes")
    groups = definition.get("transition_groups")
    edges = definition.get("edges")
    if (
        not isinstance(nodes, list)
        or len(nodes) > 4096
        or not isinstance(groups, list)
        or len(groups) > 8192
        or not isinstance(edges, list)
        or len(edges) > 16384
        or any(not isinstance(row, dict) for row in (*nodes, *groups, *edges))
    ):
        raise SpecError("native delivery graph exceeds its structural bound")
    node_keys_by_id = {}
    seen_node_keys = set()
    for node in nodes:
        identifier = node.get("id")
        key = node.get("key")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in node_keys_by_id
            or not isinstance(key, str)
            or not _PACKET_KEY.fullmatch(key)
            or key in seen_node_keys
        ):
            raise SpecError("native delivery node identity is invalid")
        node_keys_by_id[identifier] = key
        seen_node_keys.add(key)
    groups_by_id = {}
    for group in groups:
        identifier = group.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in groups_by_id:
            raise SpecError("native delivery transition-group identity is invalid")
        groups_by_id[identifier] = group
    result = []
    edge_ids = set()
    for edge in edges:
        identifier = edge.get("id")
        group = groups_by_id.get(edge.get("transition_group_id"))
        parameters = edge.get("parameters")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in edge_ids
            or not isinstance(group, dict)
            or not isinstance(parameters, list)
            or any(
                not isinstance(parameter, dict)
                or not isinstance(parameter.get("key"), str)
                or not _PACKET_KEY.fullmatch(parameter["key"])
                for parameter in parameters
            )
        ):
            raise SpecError("native delivery transition edge is invalid")
        edge_ids.add(identifier)
        fields = [parameter["key"] for parameter in parameters]
        if len(fields) != len(set(fields)):
            raise SpecError("native delivery transition repeats a field")
        source_id = group.get("source_node_id")
        transition = group.get("transition_id")
        target_id = edge.get("target_node_id")
        source = node_keys_by_id.get(source_id)
        target = node_keys_by_id.get(target_id)
        if (
            source is None
            or target is None
            or not isinstance(transition, str)
            or not transition
        ):
            raise SpecError("native delivery transition identity is incomplete")
        result.append({
            "id": identifier,
            "source": source,
            "transition": transition,
            "target": target,
            "fields": frozenset(fields),
        })
    return result


def _post_pr_transitions(graph: dict) -> list[dict]:
    return [
        edge for edge in _native_delivery_transitions(graph)
        if edge["fields"] & _PR_TARGET_CARRIER_FIELDS
        and not (
            edge["source"] == "prepare_pr"
            and edge["transition"] == "prepare_pr_no_pr"
        )
    ]


def _target_branch_graph(profile: ProjectProfile, graph: dict) -> bool:
    selected = _delivery_target_family(profile)
    post_pr = _post_pr_transitions(graph)
    carries_target = any("pr_base_branch" in edge["fields"] for edge in post_pr)
    if not selected:
        if carries_target:
            raise SpecError(
                "legacy delivery profile is paired with a target-branch graph"
            )
        return False
    if profile.capability("pull_requests"):
        if not post_pr or any(
            "pr_base_branch" not in edge["fields"] for edge in post_pr
        ):
            raise SpecError(
                "current delivery profile lacks closed PR target carriers"
            )
    elif carries_target:
        raise SpecError(
            "non-PR delivery profile unexpectedly declares a PR target carrier"
        )
    return True


def _delivery_target_family(profile: ProjectProfile) -> bool:
    return bool(
        profile.delivery_reliability_policy() == "v1"
        and _delivery_context_enabled(profile)
        and profile.capability("pull_requests")
        and (
            profile.schema_version == 3
            or profile.runtime_contracts_v2_1()
        )
    )


def _current_profile_for_pack(root: Path) -> ProjectProfile | None:
    """Read the current profile without following checkout-controlled links."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        root_fd = os.open(root, directory_flags)
        descriptors.append(root_fd)
        try:
            kent_fd = os.open(".kent", directory_flags, dir_fd=root_fd)
        except FileNotFoundError:
            return None
        descriptors.append(kent_fd)
        try:
            profile_fd = os.open(
                "workflow-profile.toml",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=kent_fd,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(profile_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or before.st_mode & 0o7022
                or before.st_size > 64 * 1024
            ):
                raise ValueError("unsafe current delivery profile")
            raw = stream.read(64 * 1024 + 1)
            after = os.fstat(stream.fileno())
            if (
                len(raw) > 64 * 1024
                or len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ValueError("current delivery profile changed during read")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    profile = ProjectProfile.from_toml(
        root,
        raw.decode("utf-8"),
        source=str(root / ".kent" / "workflow-profile.toml"),
    )
    if profile.project_root != root:
        raise ValueError("delivery profile belongs to another workspace")
    return profile


def delivery_context_schema(
    observation: ObservedScriptDeliveryGraph,
) -> str | None:
    """Select the closed context schema only from a fresh native graph."""
    profile, _, _, graph = _observed_delivery_values(observation)
    if not _delivery_context_enabled(profile):
        if any(
            "delivery_context" in edge["fields"]
            for edge in _native_delivery_transitions(graph)
        ):
            raise SpecError(
                "native graph declares delivery_context outside its profile contract"
            )
        return None
    latest = _target_branch_graph(profile, graph)
    return (
        "workflow-delivery-context-v2"
        if latest else "workflow-delivery-context-v1"
    )


def pr_base_branch_required(
    observation: ObservedScriptDeliveryGraph,
) -> bool:
    """Require target identity only for the current profile/native graph pair."""
    profile, _, _, graph = _observed_delivery_values(observation)
    return _target_branch_graph(profile, graph)


def prepare_ci_protocol(
    observation: ObservedScriptDeliveryGraph,
) -> str | None:
    """Select legacy or typed diagnostic routing from current profile and graph."""
    profile, _, _, graph = _observed_delivery_values(observation)
    transitions = _native_delivery_transitions(graph)
    typed_route_names = {
        "ci_monitor_retry_preparation",
        "ci_monitor_preparation_needs_user_action",
    }
    typed_route_present = any(
        edge["transition"] in typed_route_names
        or "ci_preparation_diagnostic" in edge["fields"]
        for edge in transitions
    )
    source_ci_enabled = (
        profile.source_ci_contract()
        and profile.capability("ci_monitoring")
    )
    if not source_ci_enabled:
        if typed_route_present:
            raise SpecError(
                "native graph declares typed CI preparation routes outside its profile contract"
            )
        return None
    failures = [
        edge for edge in transitions
        if edge["source"] == "ci_prepare"
        and edge["transition"] == "ci_prepare_failed"
        and edge["target"] == "ci_monitor"
    ]
    if len(failures) != 1:
        raise SpecError("native CI preparation failure route is missing or ambiguous")
    if profile.source_ci_contract_v3():
        required = {
            "workspace_path", "task_short_id", "ci_preparation_diagnostic",
        }
        if failures[0]["fields"] != required:
            raise SpecError(
                "current CI preparation diagnostic carrier differs from its contract"
            )
        retries = [
            edge for edge in transitions
            if edge["source"] == "ci_monitor"
            and edge["transition"] == "ci_monitor_retry_preparation"
            and edge["target"] == "ci_prepare"
        ]
        needs_action = [
            edge for edge in transitions
            if edge["source"] == "ci_monitor"
            and edge["transition"]
            == "ci_monitor_preparation_needs_user_action"
            and edge["target"] == "ci_monitor"
        ]
        if (
            len(retries) != 1
            or retries[0]["fields"] != {
                "workspace_path", "task_short_id", "delivery_context",
            }
            or len(needs_action) != 1
            or needs_action[0]["fields"] != {
                "workspace_path", "task_short_id",
                "ci_preparation_diagnostic", "blocker_reason",
            }
        ):
            raise SpecError("native CI preparation recovery routes differ from their contract")
        return "typed-v1"
    if "ci_preparation_diagnostic" in failures[0]["fields"]:
        raise SpecError("legacy CI profile is paired with a typed diagnostic route")
    return "legacy"


def source_admission_gate(admission: dict[str, object], *,
                          project_id: str, workflow_id: str, project_root: str) -> str:
    """Render already validated deployment inputs without I/O or authority checks."""
    if admission.get("schema") == "delivery-source-admission-v2":
        return _source_admission_gate_v2(
            admission,
            project_id=project_id,
            workflow_id=workflow_id,
            project_root=project_root,
        )
    canonical = json.dumps(admission, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    receipt = admission["receipt_ref"]
    command = [
        "python3", str(Path(admission["kit_root"]) / "scripts/inspect-delivery-source"),
        "--receipt", receipt["path"], "--receipt-sha256", receipt["sha256"],
        "--project-id", project_id, "--workflow-id", workflow_id,
        "--project-root", project_root, "--kit-root", admission["kit_root"],
        "--primary-root", admission["primary_root"],
        "--source-ref", admission["source_ref"], "--kit-ref", admission["kit_ref"],
        "--ci-helper", admission["ci_helper"],
    ]
    for helper in sorted(admission["helper_bindings"], key=lambda item: item["link"]):
        command.extend(("--installed-helper", helper["link"], helper["target"]))
    marker = json.dumps({
        "source_admission_sha256": hashlib.sha256(canonical).hexdigest(),
        "receipt_sha256": receipt["sha256"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    invocation = shlex.join(command) + ' --workspace "$(git rev-parse --show-toplevel)"'
    return f"""{SOURCE_ADMISSION_BEGIN}
{marker}
Published source admission comes before project-local helpers. At first storage
ingress, use the qualified installed project-memory adapter's initialize-task
interface with the actual Git root and inherited native Session identity.
Supply the exact Task locator when available; never impersonate another Session.
The adapter selects task-fixed legacy mode before R2 source inspection.

An inventoried legacy Task retains its original infrastructure, paths, accepted
contract and effect limits; do not impose this package on its old checkout.
For an admitted R2 Task, run the exact read-only guard:

```sh
{invocation}
```

Proceed only on verified admission. A receipt proves byte closure, not Task
approval, reader compatibility or permission for an external effect.
Missing identity, source drift or incompatible retained readers requires
preserving state and reporting the concrete gap. Do not fetch, provision,
select another baseline or create a .todo fallback. This check is procedural,
not an engine sandbox or a reservation against subsequent source changes.
{SOURCE_ADMISSION_END}"""


def admit_script_source_ingress(root, native_context, *, kent_path):
    """Expose current native Script source admission through the delivery API."""
    from .operations import admit_script_source_ingress as admit

    return admit(root, native_context, kent_path=kent_path)


def _source_admission_gate_v2(
    admission: dict[str, object],
    *,
    project_id: str,
    workflow_id: str,
    project_root: str,
) -> str:
    """Render the closed full V2 native ingress frame; V1 remains byte-stable."""
    expected = {
        "schema", "receipt_ref", "primary_workspace_id", "primary_root",
        "kit_root", "source_ref", "kit_ref", "ci_helper", "helper_bindings",
        "binding", "ingress",
    }
    if set(admission) != expected:
        raise ValueError("source admission v2 fields are invalid")
    binding = admission["binding"]
    ingress = admission["ingress"]
    receipt = admission["receipt_ref"]
    helpers = admission["helper_bindings"]
    if not isinstance(binding, dict) or not isinstance(ingress, dict):
        raise ValueError("source admission v2 binding or ingress is invalid")
    if binding == {"kind": "plan-publication"}:
        pass
    elif (
        set(binding) in (
            {"kind", "installation_binding_sha256"},
            {"kind", "installation_binding_sha256", "receipt_workflow_id"},
        )
        and binding.get("kind") == "r2-installed"
        and isinstance(binding.get("installation_binding_sha256"), str)
    ):
        pass
    else:
        raise ValueError("source admission v2 binding variant is invalid")
    if "receipt_workflow_id" in binding:
        try:
            owner = str(uuid.UUID(binding["receipt_workflow_id"]))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("source admission receipt owner is invalid") from error
        if owner != binding["receipt_workflow_id"]:
            raise ValueError("source admission receipt owner is invalid")
    if (
        set(ingress) != {"kind", "edge_id"}
        or ingress.get("kind") not in {"agent-edge", "script-edge"}
        or not isinstance(ingress.get("edge_id"), str)
        or not ingress["edge_id"]
    ):
        raise ValueError("source admission v2 ingress is invalid")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"path", "sha256"}
        or not isinstance(receipt.get("path"), str)
        or not isinstance(receipt.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])
        or not isinstance(helpers, list)
        or any(
            not isinstance(helper, dict)
            or set(helper) != {"link", "target"}
            or not isinstance(helper.get("link"), str)
            or not isinstance(helper.get("target"), str)
            for helper in helpers
        )
    ):
        raise ValueError("source admission v2 receipt or helper binding is invalid")
    if any(not isinstance(admission[key], str) or not admission[key]
           for key in (
               "primary_workspace_id", "primary_root", "kit_root",
               "source_ref", "kit_ref", "ci_helper",
           )):
        raise ValueError("source admission v2 input is invalid")

    frame = {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "source_admission": admission,
    }
    frame_text = json.dumps(
        frame, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    command = [
        "python3", str(Path(admission["kit_root"]) / "scripts/inspect-delivery-source"),
        "--receipt", receipt["path"], "--receipt-sha256", receipt["sha256"],
        "--project-id", project_id,
        "--workflow-id", binding.get("receipt_workflow_id", workflow_id),
        "--project-root", project_root, "--kit-root", admission["kit_root"],
        "--primary-root", admission["primary_root"],
        "--source-ref", admission["source_ref"], "--kit-ref", admission["kit_ref"],
        "--ci-helper", admission["ci_helper"],
    ]
    for helper in sorted(helpers, key=lambda item: item["link"]):
        command.extend(("--installed-helper", helper["link"], helper["target"]))
    invocation = shlex.join(command) + ' --workspace "$(git rev-parse --show-toplevel)"'
    if binding["kind"] == "r2-installed":
        ingress_guidance = """Use the trusted installed project-memory entrypoint
with the actual Git root and inherited native Session identity. It verifies
native identity and the installed binding/profile, then selects task-fixed mode.
For R2, it admits the pinned source before any initialize-task write or
project-local helper/document access. Only then initialize a new R2 Task, or
resolve its existing binding. An exact interrupted initialization may retry.
Inventoried legacy Tasks retain their original infrastructure and accepted
contracts; do not impose the current R2 closure on their historical checkout.
The outer frame identifies the target Workflow/ingress; receipt_workflow_id,
when present, identifies only the installed receipt owner."""
    else:
        ingress_guidance = """This is a plan-publication source admission only.
Do not initialize an R2 Task, call project-memory, perform a CAS write, or
create a legacy fallback."""
    return f"""{SOURCE_ADMISSION_BEGIN}
{frame_text}
Published source admission comes before project-local helpers. At first
storage ingress, use the exact mode bound by source_admission.binding.
Before initialization or project-local access, inspect the actual native Task
and complete graph through trusted Kent interfaces. Require the exact outer
project/target Workflow, source Workspace and bound ingress edge/node, matching
Task/graph versions and genuine current Agent Session or native Script ownership
as applicable. The receipt owner never substitutes the target Workflow.
{ingress_guidance}
For the admitted source, run the exact read-only guard:

```sh
{invocation}
```

Proceed only on verified admission. A receipt proves byte closure, not Task
approval, reader compatibility or permission for an external effect. Missing
identity, source drift or incompatible retained readers requires preserving
state and reporting the concrete gap. Do not fetch, provision, select another
baseline or create a .todo fallback to bypass admission. This does not prohibit
later explicitly authorized Workflow operations. This check is procedural, not an engine
sandbox or a reservation against subsequent source changes.
{SOURCE_ADMISSION_END}"""


def packet_json(value: object) -> bytes:
    """One deterministic wire encoding; string values are never normalized."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > PACKET_LIMIT:
        raise ValueError("producer artifact exceeds its byte limit")
    return raw


QUALIFIED_INITIAL_SOURCE = "KENT_QUALIFIED_INITIAL_SOURCE_V1 "


def validate_initial_source_selector(graph: dict, execution_target: str) -> dict | None:
    """Check only the qualified initial header emitted by this renderer."""
    found = []
    for collection in ("nodes", "edges", "node_groups", "transition_groups"):
        for row in graph.get(collection, []):
            for field in ("prompt", "prompt_template"):
                prompt = row.get(field)
                if not isinstance(prompt, str):
                    continue
                for line in prompt.splitlines():
                    if line.startswith(QUALIFIED_INITIAL_SOURCE):
                        found.append(line[len(QUALIFIED_INITIAL_SOURCE):])
    if not found:
        return  # Historical headers and head/default-branch policies are unchanged.
    if len(found) != 1:
        raise ValueError("qualified initial source header is duplicated")
    target = packet_decode(found[0].encode("utf-8"))
    if (
        not isinstance(target, dict)
        or set(target) != {"commit_oid", "tree_oid"}
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
               for value in target.values())
        or not isinstance(execution_target, str)
        or target["commit_oid"] != execution_target.removeprefix("ref:")
    ):
        raise ValueError("qualified initial commit/tree and execution selector disagree")
    return target


def render_source_ingress(
    capture: dict, *, ingress: dict, admission: object,
    reviewed_prefix: str, reviewed_prefix_sha256: str, qualified_target: dict,
) -> dict:
    """Pure complete-candidate transform; replace one reviewed authority prefix.

    Native/source/target qualification is the caller's responsibility. This
    function performs no native, Git, storage, installation or publication I/O.
    """
    from .operations import VerifiedSourceAdmission, PlanValidationError, _v2_exact_ingress_edge

    if not isinstance(admission, VerifiedSourceAdmission):
        raise ValueError("ingress rendering requires VerifiedSourceAdmission")
    value = admission.value
    if (
        value.get("schema") != "delivery-source-admission-v2"
        or value.get("ingress") != ingress
        or ingress.get("kind") != "agent-edge"
        or hashlib.sha256(packet_json(value)).hexdigest() != admission.source_admission_sha256
        or admission.gate != source_admission_gate(
            value, project_id=admission.project_id, workflow_id=admission.workflow_id,
            project_root=admission.project_root,
        )
    ):
        raise ValueError("verified admission does not bind the exact ingress")
    if (
        not isinstance(capture, dict)
        or set(capture) != {
            "workflow_id", "expected_version", "graph", "execution_target_policy",
        }
        or capture["workflow_id"] != admission.workflow_id
        or type(capture["expected_version"]) is not int
        or capture["expected_version"] < 0
        or not isinstance(capture["execution_target_policy"], dict)
        or not isinstance(capture["graph"], dict)
        or set(capture["graph"]) != {"nodes", "edges", "node_groups", "transition_groups"}
        or not isinstance(qualified_target, dict)
        or set(qualified_target) != {"commit_oid", "tree_oid"}
        or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{40}", v)
               for v in qualified_target.values())
        or not isinstance(reviewed_prefix, str) or not reviewed_prefix
        or len(reviewed_prefix.encode("utf-8")) > 128 * 1024
        or hashlib.sha256(reviewed_prefix.encode("utf-8")).hexdigest() != reviewed_prefix_sha256
    ):
        raise ValueError("ingress capture, reviewed prefix or qualified target is invalid")
    raw = packet_json(capture)
    result = packet_decode(raw)
    graph = result["graph"]
    if any(not isinstance(rows, list) or len(rows) > 4096
           or any(not isinstance(row, dict) for row in rows)
           for rows in graph.values()):
        raise ValueError("ingress graph collections are invalid")
    identities = [row.get("id") for rows in graph.values() for row in rows]
    if (
        any(not isinstance(identity, str) or not identity for identity in identities)
        or len(identities) != len(set(identities))
    ):
        raise ValueError("ingress graph identities are absent or duplicated")
    try:
        edge = _v2_exact_ingress_edge(graph, ingress, "ingress candidate")
    except PlanValidationError as error:
        raise ValueError(str(error)) from error
    prompt = edge.get("prompt_template")
    # Retain the initial native ownership/root guard as well as byte closure.
    # A receipt alone is not proof that this Session owns this Task ingress.
    node = next(row for row in graph["nodes"] if row["id"] == edge["target_node_id"])
    parents = re.findall(r"^Managed Task parent: (.+)$", reviewed_prefix, re.MULTILINE)
    if len(parents) > 1 or (parents and (
        not parents[0].startswith("/") or ".." in Path(parents[0]).parts
    )):
        raise ValueError("reviewed managed Task parent is invalid")
    parent_guard = (
        f"Require the canonical Task worktree strictly below {parents[0]}; "
        "never use the primary, source-cohort or artifact root. "
        if parents else ""
    )
    prefix = (
        admission.gate + "\n\n" + QUALIFIED_INITIAL_SOURCE
        + packet_json(qualified_target).decode("utf-8")
        + f"\nExpected native project: {admission.project_id}\n"
        f"Expected target Workflow: {admission.workflow_id}\n"
        f"Expected source Workspace: {admission.primary_workspace_id}\n"
        f"Expected ingress node: {node['id']}\n"
        "Before project-local helpers/documents, independently inspect the actual "
        "Task and complete native graph through trusted Kent interfaces. Require "
        "Task short_id={{.TaskShortId}}, project_id, workflow_id and source_workspace_id "
        "to equal these bindings; require the exact expected current node and genuine "
        "inherited Session ownership. Require native graph version to match the Task's "
        "Workflow version and the expected node kind/ingress edge. Do not replace native "
        "Session/Step identity. Read exact Session worktree status and require its "
        "workspace identity, canonical Task worktree_path, actual Git root and effective "
        "working directory to agree, with no availability or worktree problems. "
        + parent_guard
        + "At this initial ingress, verify the resolved native execution target and "
        "actual HEAD/HEAD^{tree} against the exact commit/tree above before local "
        "helpers or documents. Require exact tracked/index contents and no unexpected "
        "nonignored untracked source; ignored setup output is not source qualification. "
        "On identity/source uncertainty preserve state and use the existing recovery "
        "route with the concrete gap; do not reset, retarget or weaken admission. "
        "This is an initial admission, not a permanent HEAD "
        "pin; later authorized Task commits preserve their accepted baseline.\n\n"
    )
    if not isinstance(prompt, str):
        raise ValueError("exact ingress prompt is absent")
    if prompt.startswith(prefix):
        suffix = prompt[len(prefix):]
        validate_initial_source_selector(graph, result["execution_target_policy"].get("custom_ref"))
    elif prompt.startswith(reviewed_prefix):
        suffix = prompt[len(reviewed_prefix):]
    else:
        raise ValueError("ingress authority prefix differs from the reviewed preimage")
    # No hidden frame, duplicate marker or leftover commit/tree authority.
    for collection, rows in graph.items():
        for row in rows:
            for key in ("prompt", "prompt_template"):
                text = suffix if row is edge and key == "prompt_template" else row.get(key, "")
                if not isinstance(text, str):
                    continue
                if any(marker in text for marker in (
                    SOURCE_ADMISSION_BEGIN, SOURCE_ADMISSION_END, QUALIFIED_INITIAL_SOURCE,
                )):
                    raise ValueError("duplicate source authority outside the exact prefix")
                if row is edge and key == "prompt_template" and re.search(
                    r"Qualified initial (?:commit|Git tree):|commit_oid=[0-9a-f]{40}|HEAD=[0-9a-f]{40}",
                    text,
                ):
                    raise ValueError("contradictory initial commit/tree authority remains")
    edge["prompt_template"] = prefix + suffix
    result["execution_target_policy"] = {
        "mode": "custom_ref", "custom_ref": qualified_target["commit_oid"],
    }
    validate_initial_source_selector(graph, qualified_target["commit_oid"])
    packet_json(result)
    return result


def packet_decode(raw: bytes) -> dict:
    if len(raw) > PACKET_LIMIT:
        raise ValueError("producer artifact exceeds its byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate producer artifact key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("producer artifact must be an object")
    return value


def _packet_strings(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not _PACKET_KEY.fullmatch(k)
        or not isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"{label} must contain only simple string fields")
    if _CONTROL_KEYS.intersection(value):
        raise ValueError(f"{label} contains native identity/control overrides")
    for item in value.values():
        item.encode("utf-8")
    return dict(value)


def _packet_identity(root: Path, identity: dict) -> dict:
    if not isinstance(identity, dict) or set(identity) not in (
        _IDENTITY_KEYS, _LEGACY_IDENTITY_KEYS, _SCRIPT_IDENTITY_KEYS,
    ):
        raise ValueError("producer identity is incomplete")
    strings = dict(identity)
    if set(identity) == _SCRIPT_IDENTITY_KEYS:
        branch = strings.pop("transition_branch_key")
        if (identity["invocation_kind"] != "script"
                or (branch is not None and (not isinstance(branch, str) or not branch))):
            raise ValueError("invalid native Script invocation identity")
    if any(not isinstance(v, str) or not v for v in strings.values()):
        raise ValueError("producer identity is incomplete")
    if identity["workspace_path"] != str(root) or not root.is_absolute():
        raise ValueError("producer workspace identity mismatch")
    if not _PACKET_KEY.fullmatch(identity["task_short_id"]):
        raise ValueError("invalid producer task key")
    if set(identity) == _IDENTITY_KEYS:
        from .runtime import _evidence_owner
        _evidence_owner({key: identity[key] for key in ("session_id", "step_id")}, version=2)
    return dict(identity)


def _script_path(root: Path, value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("native Script path is unavailable")
    path = Path(value)
    # Graphs may own qualified absolute helpers outside the source worktree.
    # Normalize relative paths against the actual execution workspace.
    return os.path.normpath(str(path if path.is_absolute() else root / path))


def observe_script_identity(root: Path, task: str | None, native_context: dict, *,
                            native_read, return_details: bool = False) -> dict:
    """Observe one native Script invocation from actual injected ``_kent``.

    ``native_read`` is a trusted adapter's bounded read-only native CLI reader,
    with (root, fixed_arguments, structured=True). No identity or arbitrary
    command may come from a user packing request. Agent environment, including
    unrelated inherited Session IDs, is not used as Script invocation proof.
    """
    if (not root.is_absolute() or root.resolve() != root
            or (task is not None and (not isinstance(task, str) or not _PACKET_KEY.fullmatch(task)))):
        raise ValueError("native Script workspace/Task is invalid")
    if (not isinstance(native_context, dict) or set(native_context) not in (
        {"task_id", "node_id"}, {"task_id", "node_id", "transition_branch_key"},
    ) or any((not isinstance(value, str) or not value or len(value) > 256)
             for key, value in native_context.items()
             if not (key == "transition_branch_key" and value is None))):
        raise ValueError("invalid native Script context")
    project = native_read(root, ["project", str(root)], structured=False)
    if not isinstance(project, str) or not _PACKET_KEY.fullmatch(project):
        raise ValueError("native Script project is unresolved")
    selector = task if task is not None else native_context["task_id"]
    detail = native_read(root, ["task", "show", selector, "--project", project, "--json"])
    if not isinstance(detail, dict):
        raise ValueError("native Script Task readback is invalid")
    summary, source = detail.get("summary"), detail.get("source_workspace")
    if (not isinstance(summary, dict) or summary.get("id") != native_context["task_id"]
            or (task is not None and summary.get("short_id") != task)
            or not isinstance(summary.get("short_id"), str)
            or not _PACKET_KEY.fullmatch(summary["short_id"])
            or summary.get("project_id") != project
            or not isinstance(source, dict) or source.get("is_primary") is not True
            or not isinstance(source.get("workspace_id"), str) or not source["workspace_id"]
            or not isinstance(source.get("root_path"), str)):
        raise ValueError("native Script Task/project identity mismatch")
    source_root = Path(source["root_path"])
    project_detail = detail.get("project", {})
    if (not source_root.is_absolute() or source_root.resolve() != source_root
            or not isinstance(project_detail, dict)
            or project_detail.get("default_workspace_id", source["workspace_id"]) != source["workspace_id"]
            or (detail.get("worktree_path") or source["root_path"]) != str(root)
            or native_read(root, ["project", str(source_root)], structured=False) != project):
        raise ValueError("native Script workspace/source identity mismatch")
    scripts = detail.get("current_scripts")
    if (not isinstance(scripts, list) or len(scripts) > 256
            or any(not isinstance(row, dict) or not isinstance(row.get("current_node"), dict)
                   for row in scripts)):
        raise ValueError("native Script observation is incomplete")
    matches = [
        row for row in scripts
        if row["current_node"].get("node_id") == native_context["node_id"]
        and row["current_node"].get("transition_branch_key") == native_context.get("transition_branch_key")
    ]
    if len(matches) != 1 or matches[0]["current_node"].get("session_id"):
        raise ValueError("native current Script ownership is unresolved")
    workflow = summary.get("workflow_id")
    if not isinstance(workflow, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", workflow,
    ):
        raise ValueError("native Script Workflow is unresolved")
    graph = native_read(root, ["workflow", "graph", "inspect", workflow, "--json"])
    if (not isinstance(graph, dict) or graph.get("workflow_id") != workflow
            or not isinstance(graph.get("graph"), dict)
            or not isinstance(graph["graph"].get("nodes"), list)
            or any(not isinstance(node, dict) for node in graph["graph"]["nodes"])):
        raise ValueError("native Script graph identity mismatch")
    nodes = [node for node in graph["graph"]["nodes"]
             if node.get("id") == native_context["node_id"]]
    if (len(nodes) != 1 or nodes[0].get("kind") != "script"
            or _script_path(root, nodes[0].get("script_path"))
            != _script_path(root, matches[0].get("path"))):
        raise ValueError("native graph does not own the current Script path")
    identity = _packet_identity(root, {
        "task_id": summary["id"], "task_short_id": summary["short_id"], "project_id": project,
        "workspace_path": str(root), "invocation_kind": "script",
        "node_id": native_context["node_id"],
        "transition_branch_key": native_context.get("transition_branch_key"),
    })
    if return_details:
        return identity, detail, graph
    return identity


def observe_script_delivery_graph(
    root: Path,
    task: str | None,
    native_context: dict,
    *,
    native_read,
) -> ObservedScriptDeliveryGraph:
    """Observe current native Task/graph; never accept caller graph bytes."""
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("native Script workspace is invalid")
    profile = ProjectProfile.load(root)
    identity, fresh_detail, graph = observe_script_identity(
        root,
        task,
        native_context,
        native_read=native_read,
        return_details=True,
    )
    if profile.project_root != root:
        raise ValueError("native Script profile belongs to another workspace")

    return ObservedScriptDeliveryGraph(
        identity=identity,
        task_detail=fresh_detail,
        graph=graph,
        profile=profile,
        _token=_OBSERVED_SCRIPT_DELIVERY_TOKEN,
    )


def capture_script_producer(root: Path, *, task: str | None, native_context: dict,
                            producer_kind: str, inputs: dict[str, str],
                            result: dict[str, str], native_read,
                            binding: dict[str, str] | None = None,
                            github_retry_constraint: str | None = None) -> tuple[str, bytes]:
    """Actual Script adapter seam; never synthesize Agent Session/Run/Step."""
    identity = observe_script_identity(root, task, native_context, native_read=native_read)
    retained = (binding or {}).get("producer_refs")
    if retained is not None:
        try:
            reference_packet = packet_decode(retained.encode("utf-8"))
            references = reference_packet.get("refs")
        except (AttributeError, UnicodeError, ValueError) as error:
            raise ValueError("invalid Script producer references") from error
        if (
            set(reference_packet) != {"refs"}
            or not isinstance(references, list)
            or len(references) > 64
            or any(not isinstance(reference, str) for reference in references)
            or len(set(references)) != len(references)
        ):
            raise ValueError("invalid Script producer references")
        for reference in references:
            producer_reference_closure(
                root,
                reference,
                identity=identity,
            )
    return capture_producer(root, identity=identity, producer_kind=producer_kind,
                            inputs=inputs, result=result, binding=binding,
                            github_retry_constraint=github_retry_constraint)


@contextmanager
def _packet_directory(root: Path, task: str, *, create: bool):
    """Admit each component relative to a pinned no-follow directory."""
    descriptors = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open("/", flags)
        descriptors.append(descriptor)
        for name in root.parts[1:]:
            if name in {".", ".."}:
                raise ValueError("unsafe producer workspace")
            descriptor = os.open(name, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        for index, name in enumerate(("build", "kent-workflow", task, "packets")):
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            descriptor = os.open(name, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            # build/ is shared with other build outputs; private packet ancestry
            # starts at kent-workflow and may never be silently chmod-ed.
            if info.st_uid != os.getuid() or (
                index > 1 and stat.S_IMODE(info.st_mode) != 0o700
            ) or info.st_mode & 0o022:
                raise ValueError("unsafe producer artifact directory")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    except FileNotFoundError as error:
        raise ValueError("producer artifact is missing") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _packet_read(fd: int, name: str, *, expected_links: int = 1) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=fd)
    except FileNotFoundError as error:
        raise ValueError("producer artifact is missing") from error
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != expected_links
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > PACKET_LIMIT):
            raise ValueError("unsafe producer artifact file")
        raw = stream.read(PACKET_LIMIT + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > PACKET_LIMIT or (
            info.st_size, info.st_mtime_ns, info.st_ctime_ns
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("producer artifact changed during read")
        return raw


def _packet_publish(fd: int, name: str, raw: bytes) -> None:
    """Publish complete immutable bytes under the packet-directory lock.

    A deterministic private temporary lets a retry settle a crash immediately
    after link publication without accepting arbitrary hardlinks or scanning.
    """
    temporary = "." + name + ".tmp"
    try:
        info = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink not in (1, 2)):
            raise ValueError("unsafe interrupted producer publication")
        try:
            final = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            final = None
        if final is not None:
            if ((final.st_dev, final.st_ino) != (info.st_dev, info.st_ino)
                    or info.st_nlink != 2
                    or _packet_read(fd, temporary, expected_links=2) != raw):
                raise ValueError("conflicting interrupted producer publication")
        elif info.st_nlink != 1:
            raise ValueError("unsafe interrupted producer hardlink")
        os.unlink(temporary, dir_fd=fd)
        os.fsync(fd)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd,
                    follow_symlinks=False)
        except FileExistsError:
            if _packet_read(fd, name) != raw:
                raise ValueError("conflicting immutable producer artifact")
    finally:
        os.unlink(temporary, dir_fd=fd)
        os.fsync(fd)
    if _packet_read(fd, name) != raw:
        raise ValueError("producer artifact readback mismatch")


def _packet_emitted(base: dict, locator: str) -> bytes:
    result = dict(base["result"])
    commentary = result.get("commentary", "")
    if PACKET_FOOTER.strip() in commentary:
        raise ValueError("producer commentary already contains a packet footer")
    result["commentary"] = commentary + (
        "" if not commentary or commentary.endswith("\n") else "\n"
    ) + PACKET_FOOTER + locator
    return packet_json(result)


def _prepare_producer_artifacts(
    root: Path, *, identity: dict, producer_kind: str,
    inputs: dict[str, str], result: dict[str, str],
    binding: dict[str, str] | None = None,
) -> tuple[dict, dict, str, str, bytes, bytes]:
    identity = _packet_identity(root, identity)
    if set(identity) == _LEGACY_IDENTITY_KEYS:
        raise ValueError("new Agent producer requires Session/Step identity, not Run")
    if not isinstance(producer_kind, str) or not _PACKET_KEY.fullmatch(producer_kind):
        raise ValueError("invalid producer kind")
    base = {
        "schema": (
            "workflow-producer-source-v1" if set(identity) == _SCRIPT_IDENTITY_KEYS
            else "workflow-producer-source-v2"
        ), "identity": identity,
        "producer_kind": producer_kind,
        "inputs": _packet_strings(inputs, "producer inputs"),
        "result": _packet_strings(result, "producer result"),
        "binding": _packet_strings(binding or {}, "producer binding"),
    }
    _packet_value_identity(base, identity)
    raw = packet_json(base)
    name = "base-" + hashlib.sha256(raw).hexdigest() + ".json"
    locator = f"build/kent-workflow/{identity['task_short_id']}/packets/{name}"
    emitted = _packet_emitted(base, locator)
    return identity, base, name, locator, raw, emitted


def _publish_producer_artifacts(
    root: Path, identity: dict, name: str, raw: bytes, emitted: bytes,
) -> None:
    with _packet_directory(root, identity["task_short_id"], create=True) as fd:
        _packet_publish(fd, name, raw)
        _packet_publish(fd, "emitted-" + hashlib.sha256(emitted).hexdigest() + ".json", emitted)


def capture_producer(
    root: Path, *, identity: dict, producer_kind: str,
    inputs: dict[str, str], result: dict[str, str],
    binding: dict[str, str] | None = None,
    github_retry_constraint: str | None = None,
) -> tuple[str, bytes]:
    """Trusted producer seam: observe native identity before calling this API.

    Returns a relative base locator and the exact bytes to print only after both
    immutable artifacts have passed durable publication/readback.
    """
    identity = _packet_identity(root, identity)
    bound = _capture_retry_binding(
        root,
        identity=identity,
        binding=binding,
        github_retry_constraint=github_retry_constraint,
    )
    identity, _, name, locator, raw, emitted = _prepare_producer_artifacts(
        root, identity=identity, producer_kind=producer_kind, inputs=inputs,
        result=result, binding=bound,
    )
    _publish_producer_artifacts(root, identity, name, raw, emitted)
    return locator, emitted


@contextmanager
def _producer_reader(root: Path, task: str, read_relative):
    """Only fixed validated packet locators cross the archive-reader boundary."""
    prefix = f"build/kent-workflow/{task}/packets/"

    def bounded(reader):
        def read(relative):
            if (not relative.startswith(prefix)
                    or not re.fullmatch(r"(?:base|emitted)-[0-9a-f]{64}\.json",
                                        relative[len(prefix):])):
                raise ValueError("unsafe producer artifact read locator")
            raw = reader(relative)
            if not isinstance(raw, bytes) or len(raw) > PACKET_LIMIT:
                raise ValueError("invalid or oversized producer artifact bytes")
            return raw
        return read

    if read_relative is None:
        with _packet_directory(root, task, create=False) as fd:
            yield bounded(lambda relative: _packet_read(fd, relative.rsplit("/", 1)[1]))
    else:
        yield bounded(read_relative)


def reconstruct_producer(root: Path, locator: str, *, identity: dict,
                         read_relative=None) -> dict:
    """Historical transport authenticates Task/root, not current HEAD or Run.

    An optional trusted archive reader supplies receipt-verified bytes after
    retirement. It receives only fixed validated relative packet locators; it
    does not waive digest, schema, owner or emitted-byte verification.
    """
    identity = _packet_identity(root, identity)
    match = _PACKET_LOCATOR.fullmatch(locator)
    if not match or match[1] != identity["task_short_id"]:
        raise ValueError("unsafe or foreign producer locator")
    with _producer_reader(root, match[1], read_relative) as read:
        raw = read(locator)
        if hashlib.sha256(raw).hexdigest() != match[2]:
            raise ValueError("producer artifact digest mismatch")
        base = packet_decode(raw)
        if set(base) != {"schema", "identity", "producer_kind", "inputs", "result", "binding"}:
            raise ValueError("invalid producer artifact fields")
        if base["schema"] not in {"workflow-producer-source-v1", "workflow-producer-source-v2"}:
            raise ValueError("unsupported producer artifact schema")
        stored = _packet_identity(root, base["identity"])
        if (base["schema"] == "workflow-producer-source-v2") != (set(stored) == _IDENTITY_KEYS):
            raise ValueError("inconsistent producer identity version")
        for key in ("task_id", "task_short_id", "project_id", "workspace_path"):
            if stored[key] != identity[key]:
                raise ValueError("foreign producer identity")
        if not isinstance(base["producer_kind"], str) or not _PACKET_KEY.fullmatch(base["producer_kind"]):
            raise ValueError("invalid producer kind")
        for key in ("inputs", "result", "binding"):
            _packet_strings(base[key], key)
        if "github_retry_constraint" in base["binding"]:
            _parse_github_retry_constraint(
                base["binding"]["github_retry_constraint"]
            )
        _packet_value_identity(base, identity)
        if packet_json(base) != raw:
            raise ValueError("noncanonical producer artifact")
        emitted = _packet_emitted(base, locator)
        actual = read(str(Path(locator).with_name(
            "emitted-" + hashlib.sha256(emitted).hexdigest() + ".json")))
        if actual != emitted:
            raise ValueError("emitted producer artifact mismatch")
    return {**base, "emitted": emitted, "locator": locator}


def _producer_ancestry(
    root: Path, locators: list[str], *, identity: dict, read_relative=None,
) -> tuple[dict[str, dict], int]:
    pending = list(locators)
    producers: dict[str, dict] = {}
    inspected = 0
    while pending:
        reference = pending.pop()
        if reference in producers:
            continue
        if len(producers) >= 32:
            raise ValueError("producer retention closure exceeds file limit")
        producer = reconstruct_producer(
            root, reference, identity=identity, read_relative=read_relative,
        )
        inspected += len(producer["emitted"]) + len(packet_json(producer["inputs"]))
        if inspected > 32 * 1024 * 1024:
            raise ValueError("producer retention closure exceeds byte limit")
        producers[reference] = producer
        nested = producer["binding"].get("producer_refs")
        if nested is not None:
            value = packet_decode(nested.encode("utf-8"))
            refs = value.get("refs")
            if (set(value) != {"refs"} or not isinstance(refs, list) or len(refs) > 64
                    or any(not isinstance(ref, str) for ref in refs)
                    or len(set(refs)) != len(refs)):
                raise ValueError("invalid nested producer references")
            pending.extend(refs)
    return producers, inspected


def _parse_github_retry_constraint(value: object) -> dict:
    from .runtime import parse_canonical_json, validate_github_retry_constraint

    if not isinstance(value, str) or len(value.encode("utf-8")) > 1024:
        raise ValueError("invalid GitHub provider retry binding")
    try:
        parsed = parse_canonical_json(
            value, label="GitHub provider retry binding", max_bytes=1024,
        )
        return validate_github_retry_constraint(parsed)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid GitHub provider retry binding") from error


def _historical_provider_failure(producer: dict) -> bool:
    """Fail closed for legacy terminal provider failures lacking timing."""
    kind = producer.get("producer_kind")
    result = producer.get("result")
    if not isinstance(result, dict):
        return False
    transition = result.get("transition")
    if kind == "wait-ci" and transition == "ci_watch_failed":
        raw = result.get("ci_report")
        try:
            from .runtime import (
                parse_canonical_json, validate_dynamic_ci_report,
                validate_delivery_context,
            )

            if "delivery_context" in result:
                context = validate_delivery_context(packet_decode(
                    result["delivery_context"].encode("utf-8")
                ))
                if context["phase"] != "post_pr":
                    return True
                nested = context.get("ci_report")
                if raw is not None and raw != nested:
                    return True
                raw = nested
            report = validate_dynamic_ci_report(
                parse_canonical_json(
                    raw, label="historical CI provider report",
                    max_bytes=65536,
                )
            )
        except (AttributeError, TypeError, ValueError):
            return True
        return report["attempts"][-1]["reason"] in {
            "query_failed", "query_timeout", "incomplete_observation",
            "invalid_observation", "observation_limit",
        }
    if kind == "wait-pr" and transition == "merge_watch_state_changed":
        raw = result.get("pr_report")
        if not isinstance(raw, str):
            return True
        try:
            report = packet_decode(raw.encode("utf-8"))
        except (UnicodeError, ValueError):
            return True
        return report.get("reason") == "github_query_failed"
    if kind == "prepare-ci" and transition == "ci_prepare_failed":
        raw = result.get("ci_preparation_diagnostic")
        if raw is None:
            # The legacy carrier has no failure code to distinguish transport
            # from other preparation errors, so preserve its uncertainty.
            return True
        try:
            from .runtime import parse_canonical_json

            diagnostic = parse_canonical_json(
                raw, label="historical CI preparation diagnostic",
                max_bytes=65536,
            )
        except (TypeError, ValueError):
            return True
        return (
            not isinstance(diagnostic, dict)
            or diagnostic.get("schema") != "workflow-ci-preparation-diagnostic-v1"
            or diagnostic.get("code") == "github_query_failed"
        )
    return False


def _producer_github_retry_constraints(producers: dict[str, dict]) -> list[dict]:
    constraints = []
    for producer in producers.values():
        binding = producer.get("binding")
        if not isinstance(binding, dict):
            raise ValueError("producer retry binding is invalid")
        raw = binding.get("github_retry_constraint")
        if raw is not None:
            constraints.append(_parse_github_retry_constraint(raw))
        elif _historical_provider_failure(producer):
            constraints.append({
                "schema": "github-provider-retry-v1",
                "provider_host": "github.com",
                "kind": "unknown",
            })
    return constraints


def _capture_retry_binding(
    root: Path,
    *,
    identity: dict,
    binding: dict[str, str] | None,
    github_retry_constraint: str | None,
) -> dict[str, str]:
    from .runtime import canonical_bytes, merge_github_retry_constraints

    retained = _packet_strings(binding or {}, "producer binding")
    constraints = []
    if "github_retry_constraint" in retained:
        constraints.append(
            _parse_github_retry_constraint(retained["github_retry_constraint"])
        )
    if github_retry_constraint is not None:
        constraints.append(_parse_github_retry_constraint(github_retry_constraint))

    reference_text = retained.get("producer_refs")
    if reference_text is not None:
        try:
            reference_packet = packet_decode(reference_text.encode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ValueError("invalid Script producer references") from error
        references = reference_packet.get("refs")
        if (
            set(reference_packet) != {"refs"}
            or not isinstance(references, list)
            or len(references) > 64
            or any(not isinstance(reference, str) for reference in references)
            or len(set(references)) != len(references)
        ):
            raise ValueError("invalid Script producer references")
        if references:
            producers, _ = _producer_ancestry(
                root, references, identity=identity,
            )
            constraints.extend(_producer_github_retry_constraints(producers))

    merged = merge_github_retry_constraints(constraints)
    if merged is not None:
        retained["github_retry_constraint"] = canonical_bytes(merged).decode("utf-8")
    return retained


class GitHubProviderRetryBlocked(RuntimeError):
    """Fixed safe refusal with no provider response or retry deadline."""

    __slots__ = ("_state",)
    _MESSAGE = "GitHub provider read is blocked by inherited retry state"

    def __init__(self, state: str) -> None:
        if state not in {"not_before", "unknown"}:
            raise ValueError("invalid GitHub provider retry refusal")
        object.__setattr__(self, "_state", state)
        super().__init__(self._MESSAGE)

    @property
    def state(self) -> str:
        return self._state


class _PreparedGitHubProviderReadGuard:
    """Immutable in-memory restriction snapshot for pre/post-delete checks."""

    __slots__ = ("_constraint_json",)

    def __init__(self, constraint: dict | None) -> None:
        from .runtime import canonical_bytes

        object.__setattr__(
            self,
            "_constraint_json",
            canonical_bytes(constraint) if constraint is not None else None,
        )

    def __setattr__(self, _name, _value) -> None:
        raise AttributeError("prepared GitHub provider guard is immutable")

    def __call__(self) -> None:
        from .runtime import (
            evaluate_github_retry_constraint, parse_canonical_json,
        )

        constraint = (
            parse_canonical_json(
                self._constraint_json,
                label="prepared GitHub provider retry binding",
                max_bytes=1024,
            )
            if self._constraint_json is not None
            else None
        )
        state = evaluate_github_retry_constraint(
            constraint,
            now_unix_ms=time.time_ns() // 1_000_000,
        )
        if state != "clear":
            raise GitHubProviderRetryBlocked(state)


def prepare_github_provider_read_guard(
    root: Path,
    refs: list[str],
    identity: dict,
) -> _PreparedGitHubProviderReadGuard:
    """Authenticate ancestry while present and return a clock-only callable.

    ``identity`` is either the actual current Script observation or the
    qualified current Agent identity projection. It is never synthesized from
    Task values or an unrelated process environment.
    """
    identity = _packet_identity(root, identity)
    if set(identity) not in (_IDENTITY_KEYS, _SCRIPT_IDENTITY_KEYS):
        raise ValueError("GitHub provider guard requires native Agent or Script identity")
    if (
        not isinstance(refs, list)
        or len(refs) > 64
        or any(not isinstance(reference, str) for reference in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ValueError("invalid GitHub provider producer references")
    if refs:
        producers, _ = _producer_ancestry(root, refs, identity=identity)
        constraints = _producer_github_retry_constraints(producers)
    else:
        constraints = []
    from .runtime import merge_github_retry_constraints

    return _PreparedGitHubProviderReadGuard(
        merge_github_retry_constraints(constraints)
    )


def require_github_provider_read(
    root: Path,
    refs: list[str],
    identity: dict,
) -> None:
    """Return only when exact inherited GitHub provider retry state is clear."""
    prepare_github_provider_read_guard(root, refs, identity)()


def producer_locator(commentary: str) -> str | None:
    """Legacy commentary has no locator; malformed/duplicate footers fail."""
    lines = commentary.splitlines()
    candidates = [line for line in lines if PACKET_FOOTER.strip() in line]
    if not candidates:
        return None
    if (len(candidates) != 1 or not candidates[0].startswith(PACKET_FOOTER)
            or lines[-1] != candidates[0]):
        raise ValueError("invalid or duplicate producer footer")
    locator = candidates[0][len(PACKET_FOOTER):]
    if not _PACKET_LOCATOR.fullmatch(locator):
        raise ValueError("unsafe producer locator footer")
    return locator


def producer_reference_closure(root: Path, locator: str, *, identity: dict,
                               read_relative=None) -> list[str]:
    """Return exact base/emitted files, including packed original producers."""
    producers, _ = _producer_ancestry(
        root, [locator], identity=identity, read_relative=read_relative,
    )
    paths: set[str] = set()
    for reference, producer in producers.items():
        paths.add(reference)
        paths.add(str(Path(reference).with_name(
            "emitted-" + hashlib.sha256(producer["emitted"]).hexdigest() + ".json")))
    return sorted(paths)


def _packet_value_identity(base: dict, identity: dict) -> None:
    # Inputs are observations, including rejected foreign values. Only native
    # observed identity owns the artifact. A produced completion may not adopt
    # those rejected values as its execution root or Task identity.
    for key in ("task_short_id", "workspace_path"):
        if key in base["result"] and base["result"][key] != identity[key]:
            raise ValueError("producer values override native identity")


def _go_trim_space_empty(value: str) -> bool:
    return not value or all(
        char in "\t\n\v\f\r "
        or char == "\u0085"
        or unicodedata.category(char) in {"Zs", "Zl", "Zp"}
        for char in value
    )


def _effective_transition(identity: dict, task_detail: dict, graph: dict,
                          transition: str) -> tuple[dict, set[str]]:
    """Project the complete native selected transition, not caller field lists."""
    if set(identity) != _IDENTITY_KEYS:
        raise ValueError("native completion packing requires an Agent identity")
    if not isinstance(task_detail, dict) or not isinstance(graph, dict):
        raise ValueError("native transition observation must be an object")
    summary = task_detail.get("summary", {})
    if not isinstance(summary, dict) or any(summary.get(key) != identity[expected] for key, expected in (
        ("id", "task_id"), ("short_id", "task_short_id"), ("project_id", "project_id"),
    )) or summary.get("workflow_id") != graph.get("workflow_id"):
        raise ValueError("transition contract belongs to another Task/Workflow")
    version = graph.get("expected_version")
    if type(version) is not int or version < 0:
        raise ValueError("invalid native graph version")
    rows = (task_detail.get("current_nodes") or [], task_detail.get("live_sessions") or [])
    if any(not isinstance(items, list) or len(items) > 256
           or any(not isinstance(row, dict) for row in items) for items in rows):
        raise ValueError("native current node observation is invalid")
    current = rows[0] + rows[1]
    source_ids = {
        row.get("node_id") for row in current
        if row.get("session_id") == identity["session_id"] and row.get("node_id")
    }
    if len(source_ids) != 1:
        raise ValueError("native current Session source node is unresolved")
    source_id = source_ids.pop()
    definition = graph.get("graph", {})
    if not isinstance(definition, dict) or any(
        not isinstance(definition.get(key), list)
        or any(not isinstance(item, dict) for item in definition[key])
        for key in ("nodes", "transition_groups", "edges")
    ):
        raise ValueError("native transition graph is invalid")
    nodes = [node for node in definition.get("nodes", []) if node.get("id") == source_id]
    groups = [
        group for group in definition.get("transition_groups", [])
        if group.get("source_node_id") == source_id
        and group.get("transition_id") == transition
    ]
    if len(nodes) != 1 or len(groups) != 1 or nodes[0].get("kind") != "agent":
        raise ValueError("native selected transition is missing or ambiguous")
    if not isinstance(groups[0].get("id"), str) or not groups[0]["id"]:
        raise ValueError("native transition group identity is missing")
    selected = [edge for edge in definition["edges"]
                if edge.get("transition_group_id") == groups[0]["id"]]
    if any(not isinstance(edge.get("id"), str) or not edge["id"] for edge in selected):
        raise ValueError("native selected transition edge identity is missing")
    edges = sorted(
        selected,
        key=lambda edge: edge["id"],
    )
    if not edges or len({edge["id"] for edge in edges}) != len(edges):
        raise ValueError("native selected transition edges are missing or ambiguous")
    contracts = []
    for edge in edges:
        parameters = edge.get("parameters", [])
        if not isinstance(parameters, list) or any(
            not isinstance(field, dict) or not isinstance(field.get("key"), str)
            or not _PACKET_KEY.fullmatch(field["key"])
            for field in parameters
        ):
            raise ValueError("native transition fields are invalid")
        fields = [field["key"] for field in parameters]
        if len(set(fields)) != len(fields):
            raise ValueError("native transition repeats a field")
        contracts.append(set(fields))
    if any(fields != contracts[0] for fields in contracts):
        raise ValueError("native fan-out transition field contracts disagree")
    projection = {"source": nodes[0], "group": groups[0], "edges": edges}
    return {
        "task_id": identity["task_id"], "workflow_id": graph["workflow_id"],
        "expected_version": version, "source_node_id": source_id,
        "transition": transition,
        "contract_sha256": hashlib.sha256(packet_json(projection)).hexdigest(),
    }, contracts[0]


def transition_contract_reference(identity: dict, task_detail: dict, graph: dict,
                                  transition: str) -> dict:
    """Freeze a contract from actual readbacks; use again after native refresh."""
    return _effective_transition(identity, task_detail, graph, transition)[0]


def _validated_feedback_decision(
    refs: list[str],
    producers: dict[str, dict],
    *,
    identity: dict,
    graph: dict,
    profile: ProjectProfile | None,
) -> dict[str, str] | None:
    """Admit only an exact Agent derivative of one watcher state-change result."""
    candidates = [
        reference for reference in refs
        if (
            producers[reference].get("producer_kind") == "feedback-decision"
            or "feedback_decision"
            in producers[reference].get("binding", {})
        )
    ]
    if not candidates:
        return None
    if len(candidates) != 1 or len(refs) != 1:
        raise ValueError("feedback decision must be the sole packing producer")
    derivative_ref = candidates[0]
    derivative = producers[derivative_ref]
    if (
        derivative["identity"].get("session_id") != identity["session_id"]
        or derivative["identity"].get("step_id") != identity["step_id"]
    ):
        raise ValueError("feedback decision was not produced by the current Agent")
    binding = derivative["binding"]
    if (
        derivative["producer_kind"] != "feedback-decision"
        or set(binding) != {"producer_refs", "feedback_decision"}
        or set(derivative["inputs"]) != {"producer_ref", "decision", "reason"}
    ):
        raise ValueError("feedback decision derivative is incomplete")
    try:
        ancestry_packet = packet_decode(binding["producer_refs"].encode("utf-8"))
        decision = packet_decode(binding["feedback_decision"].encode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise ValueError("feedback decision binding is invalid") from error
    if (
        packet_json(ancestry_packet).decode("utf-8") != binding["producer_refs"]
        or set(ancestry_packet) != {"refs"}
        or not isinstance(ancestry_packet["refs"], list)
        or len(ancestry_packet["refs"]) != 1
        or any(not isinstance(value, str) for value in ancestry_packet["refs"])
        or packet_json(decision).decode("utf-8") != binding["feedback_decision"]
        or set(decision) != {
            "schema", "source_ref", "decision",
            "old_cursor_sha256", "new_cursor_sha256", "reason_sha256",
        }
        or decision.get("schema") != "workflow-feedback-decision-v1"
    ):
        raise ValueError("feedback decision binding is invalid")
    source_ref = ancestry_packet["refs"][0]
    if (
        not isinstance(decision.get("decision"), str)
        or not isinstance(derivative["inputs"]["reason"], str)
        or _go_trim_space_empty(derivative["inputs"]["reason"])
        or derivative["inputs"]["producer_ref"] != source_ref
        or decision["source_ref"] != source_ref
        or decision["decision"] not in {
            "acknowledge_all_reported_feedback",
            "preserve_acknowledged_cursor",
        }
        or derivative["inputs"]["decision"] != decision["decision"]
    ):
        raise ValueError("feedback decision source or choice is invalid")
    source = producers.get(source_ref)
    if source is None:
        raise ValueError("feedback decision source is not retained")
    expected_transition = {
        "wait-pr": "merge_watch_state_changed",
        "wait-ci": "ci_watch_state_changed",
    }.get(source["producer_kind"])
    if (
        expected_transition is None
        or source["result"].get("transition") != expected_transition
        or source["schema"] != "workflow-producer-source-v1"
        or source["identity"].get("invocation_kind") != "script"
    ):
        raise ValueError("feedback decision source is not a qualified watcher result")
    if profile is None:
        raise ValueError("feedback decision requires a current project profile")
    expected_source_node = {
        "wait-pr": "merge_watch",
        "wait-ci": "ci_watch",
    }[source["producer_kind"]]
    command_key = {
        "wait-pr": "wait_pr",
        "wait-ci": "wait_ci",
    }[source["producer_kind"]]
    source_nodes = [
        node for node in graph.get("graph", {}).get("nodes", [])
        if node.get("id") == source["identity"].get("node_id")
    ]
    configured_script = profile.command(command_key)
    if (
        len(source_nodes) != 1
        or source_nodes[0].get("key") != expected_source_node
        or source_nodes[0].get("kind") != "script"
        or not configured_script
        or _script_path(
            profile.project_root,
            source_nodes[0].get("script_path"),
        ) != _script_path(profile.project_root, configured_script)
    ):
        raise ValueError("feedback decision source does not match the native watcher node")
    source_edges = [
        edge for edge in _native_delivery_transitions(graph)
        if edge["source"] == expected_source_node
        and edge["transition"] == expected_transition
    ]
    if not source_edges or any(
        not {"pr_url", "pr_report", "pr_feedback_cursor"} <= edge["fields"]
        for edge in source_edges
    ):
        raise ValueError("feedback decision source transition lacks its cursor carrier")
    old_cursor = source["result"].get("pr_feedback_cursor")
    new_cursor = derivative["result"].get("pr_feedback_cursor")
    report_raw = source["result"].get("pr_report")
    if not isinstance(old_cursor, str) or not isinstance(new_cursor, str):
        raise ValueError("feedback decision cursor is missing")
    if not isinstance(report_raw, str):
        raise ValueError("feedback decision source report is missing")
    try:
        from .runtime import (
            canonical_bytes,
            validate_dynamic_pr_feedback_cursor,
        )
        from .github_observation import parse_pr_url

        def checked_cursor(raw: str) -> str:
            if raw == "uninitialized":
                return raw
            parsed = validate_dynamic_pr_feedback_cursor(raw)
            canonical = canonical_bytes(parsed).decode("utf-8")
            if canonical != raw:
                raise ValueError("feedback cursor is not canonical")
            return canonical

        old_cursor = checked_cursor(old_cursor)
        new_cursor = checked_cursor(new_cursor)
        report = packet_decode(report_raw.encode("utf-8"))
        if not isinstance(report, dict):
            raise ValueError("feedback source report is invalid")
        pr_url = source["result"].get("pr_url")
        if not isinstance(pr_url, str):
            raise ValueError("feedback source PR URL is missing")
        repository, pull_number = parse_pr_url(pr_url)
        if report.get("url") not in (None, "") and parse_pr_url(report["url"]) != (
            repository, pull_number,
        ):
            raise ValueError("feedback report URL differs from its source PR")
        candidate_raw = report.get("observed_feedback_cursor")
        if "observed_feedback_cursor" in report and not isinstance(candidate_raw, str):
            raise ValueError("feedback candidate cursor is invalid")
        candidate = checked_cursor(candidate_raw) if candidate_raw is not None else None
        for cursor in (old_cursor, new_cursor, candidate):
            if cursor is None:
                continue
            if cursor == "uninitialized":
                continue
            parsed = validate_dynamic_pr_feedback_cursor(cursor)
            if (
                parsed["repository"] != repository
                or parsed["pull_number"] != pull_number
            ):
                raise ValueError("feedback cursor belongs to another PR")
    except (KeyError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("feedback decision source evidence is invalid") from error
    if decision["decision"] == "acknowledge_all_reported_feedback":
        if candidate is None or candidate == "uninitialized" or new_cursor != candidate:
            raise ValueError("feedback decision does not acknowledge the observed cursor")
    elif new_cursor != old_cursor:
        raise ValueError("preserve decision changed the acknowledged cursor")
    reason = derivative["inputs"]["reason"]
    hashes = {
        "old_cursor_sha256": hashlib.sha256(old_cursor.encode("utf-8")).hexdigest(),
        "new_cursor_sha256": hashlib.sha256(new_cursor.encode("utf-8")).hexdigest(),
        "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    }
    if any(decision.get(key) != value for key, value in hashes.items()):
        raise ValueError("feedback decision digest binding is invalid")
    expected_result = dict(source["result"])
    expected_result["pr_feedback_cursor"] = new_cursor
    if derivative["result"] != expected_result:
        raise ValueError("feedback decision changed source report or CI bytes")
    return {
        "decision": decision["decision"],
        "source_ref": source_ref,
        "old_cursor": old_cursor,
        "new_cursor": new_cursor,
    }


def pack_delivery(root: Path, request: dict, *, identity: dict,
                  task_detail: dict, graph: dict) -> tuple[str, bytes]:
    """Pack only; caller owns native completion and success/effect freshness.

    All native observations are adapter-supplied, never request-controlled.
    No historical report is classified as fresh or passing by this transport.
    """
    identity = _packet_identity(root, identity)
    keys = {"schema", "transition", "values", "transition_contract_ref",
            "delivery_mode", "producer_refs"}
    if not isinstance(request, dict) or set(request) != keys:
        raise ValueError("invalid completion packing fields")
    if request["schema"] != "workflow-pack-delivery-v1":
        raise ValueError("unsupported completion packing schema")
    transition = request["transition"]
    if not isinstance(transition, str) or not _PACKET_KEY.fullmatch(transition):
        raise ValueError("invalid selected transition")
    actual, required = _effective_transition(identity, task_detail, graph, transition)
    reference = request["transition_contract_ref"]
    if not isinstance(reference, dict) or set(reference) != set(actual):
        raise ValueError("invalid exact transition contract reference")
    if type(reference["expected_version"]) is not int or reference["expected_version"] < 0:
        raise ValueError("invalid referenced native graph version")
    # A graph-version change alone is not drift. Exact selected-contract digest
    # and identities reconcile unchanged contracts without a version bypass.
    if any(reference[key] != value for key, value in actual.items()
           if key != "expected_version"):
        raise ValueError("selected native transition contract changed")
    if reference["expected_version"] > actual["expected_version"]:
        raise ValueError("referenced graph version is from the future")
    values = _packet_strings(request["values"], "completion values")
    if set(values) - required - {"commentary"} or "transition" in values:
        raise ValueError("completion values contain undeclared fields")
    refs = request["producer_refs"]
    if not isinstance(refs, list) or len(refs) > 64 or any(
        not isinstance(ref, str) for ref in refs
    ) or len(set(refs)) != len(refs):
        raise ValueError("invalid completion producer references")
    producers, inspected = _producer_ancestry(
        root, refs, identity=identity,
    )
    current_profile = (
        _current_profile_for_pack(root)
        if any(
            producers[reference].get("producer_kind") == "feedback-decision"
            or "feedback_decision"
            in producers[reference].get("binding", {})
            for reference in refs
        )
        else None
    )
    feedback_decision = _validated_feedback_decision(
        refs,
        producers,
        identity=identity,
        graph=graph,
        profile=current_profile,
    )
    sources = {}
    for ref in refs:
        producer = producers[ref]
        # Outputs override inputs within one actual producer invocation. Across
        # producers there is no implicit latest choice or conflict precedence.
        fields = {**producer["inputs"], **producer["result"]}
        if "delivery_context" in producer["result"]:
            from .runtime import validate_delivery_context
            produced_context = validate_delivery_context(packet_decode(
                producer["result"]["delivery_context"].encode("utf-8")
            ))
            if produced_context["phase"] == "post_pr":
                for key in (
                    "pr_url", "branch_name", "pr_base_branch", "merge_strategy",
                    "pr_feedback_cursor", "ci_contract", "ci_report",
                ):
                    if key in producer["result"]:
                        continue  # The exact flat/nested equality check follows.
                    if key in produced_context:
                        fields[key] = produced_context[key]
        for key, value in fields.items():
            if key in {"transition", "commentary"}:
                continue
            # Review synthesis is agent-owned, unlike opaque delivery/CI
            # packets. Explicit, graph-declared synthesis supersedes every
            # historical value, but never skips reconstruction of a producer.
            if key == "review_context" and key in required and key in values:
                continue
            if key in sources and sources[key] != value:
                raise ValueError("producer references disagree on exact field bytes")
            sources[key] = value
    # Expand only validated producer-owned context. A nested real report is
    # usable by later report-required routes; absence never synthesizes one.
    if "delivery_context" in sources:
        from .runtime import validate_delivery_context, delivery_context_fields
        context = validate_delivery_context(
            packet_decode(sources["delivery_context"].encode("utf-8"))
        )
        delivery_context_fields(sources)
        if context["phase"] == "post_pr":
            for key in (
                "pr_url", "branch_name", "pr_base_branch", "merge_strategy",
                "pr_feedback_cursor", "ci_contract", "ci_report",
            ):
                if key not in context:
                    continue
                if (
                    key == "pr_base_branch"
                    and request["delivery_mode"] == "post_pr_target_revalidated"
                ):
                    continue
                if (
                    key == "pr_feedback_cursor"
                    and feedback_decision is not None
                    and feedback_decision["decision"] == "acknowledge_all_reported_feedback"
                ):
                    continue
                if key in sources and sources[key] != context[key]:
                    if request["delivery_mode"] == "post_pr_target_revalidated":
                        raise ValueError("target revalidation changed non-target producer bytes")
                    raise ValueError("flat and nested post_pr producer identity disagree")
                sources[key] = context[key]
    for key in ("ci_contract", "ci_report", "delivery_context"):
        if key in values and (key not in sources or values[key] != sources[key]):
            raise ValueError("packet values require exact producer-owned bytes")
    mode = request["delivery_mode"]
    if feedback_decision is not None and mode not in {"preserve", "post_pr"}:
        raise ValueError("feedback decisions require preserve or post_pr packing")
    if mode == "preserve":
        if "delivery_context" in required and "delivery_context" not in sources:
            raise ValueError("preserve mode requires original outer delivery context")
        if (
            feedback_decision is not None
            and feedback_decision["decision"]
            == "acknowledge_all_reported_feedback"
            and "delivery_context" in required
        ):
            try:
                from .runtime import validate_delivery_context
                context = validate_delivery_context(
                    packet_decode(
                        sources["delivery_context"].encode("utf-8")
                    )
                )
            except (KeyError, UnicodeError, ValueError) as error:
                raise ValueError("feedback ACK has no valid outer context") from error
            if (
                context.get("phase") != "post_pr"
                or context.get("pr_feedback_cursor")
                != feedback_decision["new_cursor"]
            ):
                raise ValueError(
                    "feedback ACK cannot preserve a stale outer cursor; "
                    "use post_pr packing"
                )
    elif mode == "post_pr":
        if "delivery_context" in values:
            raise ValueError("post_pr mode cannot override assembled delivery context")
        profile = _current_profile_for_pack(root)
        if profile is not None:
            if not _delivery_context_enabled(profile):
                raise ValueError("post_pr packing is outside the profile contract")
            target_required = _target_branch_graph(profile, graph)
        else:
            # Generic producer fixtures and pre-profile projects retain their
            # historical field-list behavior. Qualified projects select from
            # the current profile and complete native graph above.
            target_required = "pr_base_branch" in required
        context_schema = (
            "workflow-delivery-context-v2"
            if target_required else "workflow-delivery-context-v1"
        )
        prior_raw = sources.get("delivery_context")
        if prior_raw is not None:
            try:
                from .runtime import validate_delivery_context
                prior = validate_delivery_context(
                    packet_decode(prior_raw.encode("utf-8"))
                )
            except (ValueError, UnicodeError) as error:
                raise ValueError("post_pr source context is invalid") from error
            if prior.get("phase") == "post_pr":
                if prior.get("schema") != context_schema:
                    raise ValueError(
                        "post_pr source context schema differs from current profile"
                    )
                for key in (
                    "pr_url", "branch_name", "pr_base_branch",
                    "merge_strategy", "pr_feedback_cursor",
                    "ci_contract", "ci_report",
                ):
                    observed = sources.get(key, values.get(key))
                    if (
                        observed is not None
                        and not (
                            key == "pr_feedback_cursor"
                            and feedback_decision is not None
                            and feedback_decision["decision"]
                            == "acknowledge_all_reported_feedback"
                        )
                        and prior.get(key, "") != observed
                    ):
                        raise ValueError(
                            "flat and nested post_pr producer identity disagree"
                        )
                if (
                    feedback_decision is not None
                    and feedback_decision["decision"]
                    == "acknowledge_all_reported_feedback"
                ):
                    # The derivative is the explicit authority to advance the
                    # cursor; rebuild only the outer envelope from its exact
                    # source fields, retaining opaque CI packet bytes.
                    context = {
                        "schema": context_schema,
                        "phase": "post_pr",
                    }
                else:
                    # A retry route without an ACK carries only the exact
                    # original context; do not normalize its bytes.
                    sources["delivery_context"] = prior_raw
                    context = None
            else:
                context = {
                    "schema": context_schema,
                    "phase": "post_pr",
                }
        else:
            context = {
                "schema": context_schema,
                "phase": "post_pr",
            }
        if context is None:
            pass
        else:
            context_fields = (
                "pr_url", "branch_name", "merge_strategy", "pr_feedback_cursor",
            ) + (("pr_base_branch",) if target_required else ())
            for key in context_fields:
                value = sources.get(key, values.get(key))
                if not isinstance(value, str) or not value:
                    raise ValueError("post_pr assembly lacks observed PR identity")
                if key == "pr_base_branch" and key not in sources:
                    raise ValueError("post_pr target branch must come from a producer")
                context[key] = value
            for key in ("ci_contract", "ci_report"):
                if key in sources and sources[key] != "":
                    context[key] = sources[key]
            sources["delivery_context"] = packet_json(context).decode("utf-8")
    elif mode == "post_pr_target_revalidated":
        if "delivery_context" in values or "pr_base_branch" not in required:
            raise ValueError("target revalidation requires a declared current target carrier")
        prior_raw = sources.get("delivery_context")
        observed_target = sources.get("pr_base_branch")
        if not isinstance(prior_raw, str) or not isinstance(observed_target, str) or not observed_target:
            raise ValueError("target revalidation lacks producer-owned context or branch")
        try:
            from .runtime import validate_delivery_context
            prior = validate_delivery_context(packet_decode(prior_raw.encode("utf-8")))
        except (ValueError, UnicodeError) as error:
            raise ValueError("target revalidation context is invalid") from error
        if (
            prior.get("schema") != "workflow-delivery-context-v2"
            or prior.get("phase") != "post_pr"
        ):
            raise ValueError("target revalidation requires an original v2 post-PR context")
        for key in (
            "pr_url", "branch_name", "merge_strategy", "pr_feedback_cursor",
            "ci_contract", "ci_report",
        ):
            if prior.get(key, "") != sources.get(key, ""):
                raise ValueError("target revalidation changed non-target producer bytes")
        if prior.get("pr_base_branch") == observed_target:
            raise ValueError("target revalidation did not observe a target change")
        prior["pr_base_branch"] = observed_target
        sources["delivery_context"] = packet_json(prior).decode("utf-8")
    else:
        raise ValueError("unsupported delivery packing mode")
    result = dict(values)
    for key in required:
        if key in sources:
            if key in result and result[key] != sources[key]:
                raise ValueError("completion values rewrite producer-owned strings")
            result[key] = sources[key]
    if set(result) - {"commentary"} != required:
        raise ValueError("completion is missing declared native transition fields")
    for key in required:
        if _go_trim_space_empty(result[key]):
            raise ValueError(f"required completion field {key} must be non-empty")
    if "delivery_context" in result:
        from .runtime import delivery_context_fields, validate_delivery_context
        delivery_context_fields(result)
        context = validate_delivery_context(
            packet_decode(result["delivery_context"].encode("utf-8"))
        )
        for key in (
            "pr_url", "branch_name", "merge_strategy", "pr_feedback_cursor",
            "pr_base_branch", "ci_contract", "ci_report",
        ):
            if key in context and key in result and context[key] != result[key]:
                raise ValueError("flat and nested delivery identity disagree")
    for key in ("ci_report", "ci_contract"):
        if key in result and len(result[key].encode("utf-8")) > 65536:
            raise ValueError("nested CI packet exceeds its byte limit")
    result["transition"] = transition
    binding = {
        "transition_contract_sha256": actual["contract_sha256"],
        "native_graph_version": str(actual["expected_version"]),
        "producer_refs": packet_json({"refs": refs}).decode("utf-8"),
    }
    from .runtime import canonical_bytes, merge_github_retry_constraints
    inherited_retry = merge_github_retry_constraints(
        _producer_github_retry_constraints(producers)
    )
    if inherited_retry is not None:
        binding["github_retry_constraint"] = canonical_bytes(
            inherited_retry,
        ).decode("utf-8")
    identity, base, name, locator, raw, emitted = _prepare_producer_artifacts(
        root, identity=identity, producer_kind="pack-delivery",
        inputs={}, result=result, binding=binding,
    )
    if len(producers) + 1 > 32:
        raise ValueError("producer retention closure exceeds file limit")
    inspected += len(emitted) + len(packet_json(base["inputs"]))
    if inspected > 32 * 1024 * 1024:
        raise ValueError("producer retention closure exceeds byte limit")
    _publish_producer_artifacts(root, identity, name, raw, emitted)
    return locator, emitted


WORKSPACE = ParameterSpec(
    "workspace_path",
    "Repository or managed-worktree root; never an artifact directory.",
)
REPORTED_WORKSPACE = ParameterSpec(
    "reported_workspace_path",
    "Rejected workspace value emitted by an upstream writer.",
)
PLAN = ParameterSpec(
    "plan_path",
    "Path to the approved implementation plan, or the literal not-applicable.",
)
PLAN_ROUTE = ParameterSpec(
    "plan_route",
    "Post-review route: start, continue, verify, or fix_continue.",
)
PLAN_ROUTE_CONTEXT = ParameterSpec(
    "plan_route_context",
    "Route-specific context; use not-applicable unless continuing a Fix bundle.",
)
PLAN_REVIEW_REPORT = ParameterSpec(
    "plan_review_report",
    "Independent read-only review of plan authority, scope, evidence, and ordering.",
)
PLAN_CHANGE_REPORT = ParameterSpec(
    "plan_change_report",
    "Material replanning reason: deterministic contract drift or explicitly "
    "agent-reported findings and original authority.",
)
WORK_KIND = ParameterSpec(
    "work_kind",
    "Stable project-profile work kind selected during Plan.",
)
REVIEW_CONTEXT = ParameterSpec(
    "review_context",
    "Implementation, fix, report, and artifact context required by verification.",
)
SMOKE_RATIONALE = ParameterSpec(
    "smoke_rationale",
    "Evidence-based reason that runtime smoke is required or may be skipped.",
)
SMOKE_SCOPE = ParameterSpec(
    "smoke_scope",
    "Focused runtime scenarios and surfaces that smoke testing must exercise.",
)
BLOCKER = ParameterSpec(
    "blocker_reason",
    "Exact blocker and the next user or external action required.",
)
FIX_CONTEXT = ParameterSpec(
    "fix_context",
    "Deduplicated, dependency-ordered task-scoped repair bundle for the single writer.",
)
VERIFICATION_STATUS = ParameterSpec(
    "verification_status",
    "Deterministic verification status: passed, needs_changes, or blocked.",
)
VERIFICATION_REPORT = ParameterSpec(
    "verification_report",
    "Deterministic verification command, result, and report or log location.",
)
STANDARDS_STATUS = ParameterSpec(
    "standards_status",
    "Repository standards status: passed, needs_changes, or blocked.",
)
STANDARDS_REPORT = ParameterSpec(
    "standards_report",
    "Read-only repository standards, architecture, and engineering report.",
)
COMPLIANCE_REPORT = ParameterSpec(
    "compliance_report",
    "Final delivery compliance attestation and any blocking findings.",
)
SPEC_STATUS = ParameterSpec(
    "spec_status",
    "Specification fidelity status: passed, needs_changes, or blocked.",
)
REVIEW_REPORT = ParameterSpec(
    "review_report",
    "Read-only specification and acceptance-criteria review report.",
)
PR_URL = ParameterSpec("pr_url", "Canonical pull request URL.")
BRANCH_NAME = ParameterSpec(
    "branch_name",
    "Exact non-empty current Git branch from git branch --show-current; "
    "required even when no pull request exists.",
)
MERGE_STRATEGY = ParameterSpec(
    "merge_strategy",
    "Resolved pull-request merge strategy: merge, squash, or rebase.",
)
PR_REPORT = ParameterSpec(
    "pr_report",
    "Pull request creation, review, conflict, or post-CI status report.",
)
CI_REPORT = ParameterSpec("ci_report", "CI checks and status summary.")
MERGE_REPORT = ParameterSpec(
    "merge_report",
    "Proof that the pull request is merged or equivalent delivery is complete.",
)
PUBLICATION_REPORT = ParameterSpec(
    "publication_report",
    "Exact package, version, merged source, publish command shape, and remote verification.",
)
CLOSURE_REASON = ParameterSpec(
    "closure_reason",
    "User-approved reason for closing or canceling delivery without merge.",
)
CLEANUP_REPORT = ParameterSpec(
    "cleanup_report",
    "Report of cleanup performed, skipped, or blocked.",
)
EVIDENCE_CONTEXT = ParameterSpec(
    "evidence_context",
    "Packaging-only evidence findings and exact artifacts permitted for repair.",
)
PR_HEAD_OID = ParameterSpec(
    "pr_head_oid",
    "Pull-request head commit observed after green CI.",
)
PR_BASE_OID = ParameterSpec(
    "pr_base_oid",
    "Pull-request base commit observed after green CI.",
)
PR_FEEDBACK_CURSOR = ParameterSpec(
    "pr_feedback_cursor",
    "Opaque validated cursor for incremental GitHub pull-request feedback.",
)
CLEANUP_MODE = ParameterSpec(
    "cleanup_mode",
    "Cleanup proof mode: merged, no_pr, closed_without_merge, or report_only.",
)
CLEANUP_SESSION_ID = ParameterSpec(
    "cleanup_session_id",
    "Kent session ID of the completed resource-owning Cleanup node.",
)
TASK_SHORT_ID = ParameterSpec(
    "task_short_id",
    "Stable human-readable Kent task short ID.",
)
ACCEPTED_PLAN_SHA256 = ParameterSpec(
    "accepted_plan_sha256",
    "Digest of the task-bound accepted normalized plan, or not-applicable "
    "before the first accepted snapshot.",
)
REVIEWED_NORMALIZED_SHA256 = ParameterSpec(
    "reviewed_normalized_sha256",
    "Normalized-plan SHA-256 independently reviewed before human acceptance.",
)
TARGET_ASSIGNEE = ParameterSpec(
    "target_assignee",
    "Closed coder role selected from the accepted task-bound complexity choice.",
    purpose="target_assignee",
)
TARGET_THINKING = ParameterSpec(
    "target_thinking",
    "Reasoning effort paired with the accepted task-bound coder role.",
    purpose="target_thinking",
)
CI_CONTRACT = ParameterSpec(
    "ci_contract",
    "Validated GitHub CI identity and monitoring policy for the current PR cycle.",
)
DELIVERY_CONTEXT = ParameterSpec(
    "delivery_context",
    "Closed workflow-delivery-context-v1/v2 JSON string: pre_pr, or post_pr "
    "with observed PR identity and only produced CI packets.",
)
PR_BASE_BRANCH = ParameterSpec(
    "pr_base_branch",
    "Exact freshly observed GitHub pull-request target branch.",
)
CI_PREPARATION_DIAGNOSTIC = ParameterSpec(
    "ci_preparation_diagnostic",
    "Closed diagnostic-v1 for the original CI preparation input and any "
    "validated resume context.",
)


def _delivery_context_enabled(profile: ProjectProfile) -> bool:
    return profile.runtime_contracts_v2() or (
        profile.schema_version == 3
        and profile.delivery_profile == "lite"
        and profile.capability("pull_requests")
        and bool(profile.command("github_observation"))
    )


def delivery_context_input(profile: ProjectProfile) -> str:
    if not _delivery_context_enabled(profile):
        return "\n\n"
    return (
        "\n\nDelivery context (preserve the exact JSON string): "
        "{{.Params.delivery_context}}\n\n"
    )


def delivery_context_tail(profile: ProjectProfile) -> str:
    if not _delivery_context_enabled(profile):
        return ""
    if _delivery_target_family(profile):
        return (
            "\n\nPreserve the exact incoming `delivery_context` JSON string on every "
            "outgoing transition that declares it; do not normalize or rewrite "
            "nested packet strings. On every fresh PR observation, compare the "
            "actual `baseRefName` with the carried `pr_base_branch` before "
            "reporting success, merge, or cleanup. A retarget is an explicit "
            "owner decision: require existing authority and fresh PR "
            "observation, then use `post_pr_target_revalidated` to change only "
            "the target in the v2 packet. This packer mode adds no graph edge "
            "or authority; preserve CI bytes and retry state and do not infer "
            "a target from a watcher observation."
        )
    return (
        "\n\nPreserve the exact incoming `delivery_context` JSON string on every "
        "outgoing transition that declares it; do not normalize or rewrite "
        "nested packet strings."
    )


def delivery_context_pack_instruction(profile: ProjectProfile) -> str:
    if not _delivery_context_enabled(profile):
        return "\n\n"
    if _delivery_target_family(profile):
        return """\n\nWhen a transition requires `delivery_context`, emit its compact
closed `workflow-delivery-context-v2` JSON string. Use exactly `schema` and
`phase=pre_pr` before any PR is observed. For `phase=post_pr`, include the
observed `pr_url`, `branch_name`, `pr_base_branch`, `merge_strategy`, and
`pr_feedback_cursor`. `pr_base_branch` is the exact current GitHub PR target
branch, not the task branch or an inferred default. Include `ci_contract` and
`ci_report` only when those exact packets were produced; preserve their string
values byte-for-byte. Historical v1 strings remain byte-for-byte unchanged.
Never invent absent PR or CI fields or rewrite a nested packet.\n\n"""
    return """\n\nWhen a transition requires `delivery_context`, emit its compact
closed `workflow-delivery-context-v1` JSON string. Use exactly `schema` and
`phase=pre_pr` before any PR is observed. For `phase=post_pr`, include the
observed `pr_url`, `branch_name`, `merge_strategy`, and
`pr_feedback_cursor`. Include `ci_contract` and `ci_report` only when those
exact packets were produced; preserve their string values byte-for-byte.
Never invent absent PR or CI fields or rewrite a nested packet.\n\n"""


def build_delivery_workflow(
    profile: ProjectProfile,
    version: int,
) -> WorkflowSpec:
    orchestrator = profile.role("orchestrator")
    gate = profile.optional_role("gate") or orchestrator
    implementation = profile.role("implementation")
    fix = profile.role("fix")
    release = profile.role("release")
    smoke_enabled = profile.smoke_policy() != "disabled"
    pull_requests = profile.capability("pull_requests")
    qa = profile.role("qa") if smoke_enabled else ""
    ci = profile.role("ci") if pull_requests else ""
    plan_review_role = (
        profile.optional_role("plan_review")
        or profile.optional_role("spec_review")
        or profile.role("researcher")
    )
    fresh_writers = profile.writer_session_policy() == "fresh_per_slice"
    coder_selection_enabled = profile.coder_selection_policy() == "complexity"
    planner = "complexity-planner" if coder_selection_enabled else orchestrator
    writer_handoff_context = (
        "new_session"
        if fresh_writers or coder_selection_enabled
        else "compact_and_continue_session"
    )
    branch_identity_handoff_source = (
        "immediate_source"
        if fresh_writers or coder_selection_enabled
        else "node:plan"
    )
    implementation_continuation_context = (
        "new_session" if fresh_writers else "continue_session"
    )
    plan_contract_continuation_source = (
        "immediate_source" if fresh_writers else "previous_target_or_new"
    )
    fix_context = (
        "new_session" if fresh_writers else "compact_and_continue_session"
    )
    fix_context_source = (
        "immediate_source" if fresh_writers else "previous_target_or_new"
    )
    runtime_v2 = profile.runtime_contracts_v2()
    source_ci = profile.source_ci_contract() and profile.capability("ci_monitoring")
    if (
        profile.schema_version == 4 and runtime_v2
        and profile.capability("ci_monitoring") and not source_ci
    ):
        raise SpecError(
            "schema-4 runtime-contract v2 delivery requires "
            "prepare_ci 2.0.0 source-contract adoption"
        )
    cursor_enabled = _delivery_context_enabled(profile)
    delivery_context_enabled = cursor_enabled
    target_branch_enabled = _delivery_target_family(profile)
    source_ci_v3 = (
        source_ci and profile.source_ci_contract_v3()
    )
    delivery_context_parameters = (
        (DELIVERY_CONTEXT,) if delivery_context_enabled else ()
    )
    pr_cursor_parameters = (PR_FEEDBACK_CURSOR,) if cursor_enabled else ()
    ci_contract_parameters = (
        (CI_CONTRACT, TASK_SHORT_ID)
        if source_ci
        else ((CI_CONTRACT,) if profile.capability("ci_monitoring") else ())
    )
    ci_contract_retry_parameters = ci_contract_parameters + (
        (CI_REPORT,) if profile.capability("ci_monitoring") else ()
    )
    fix_continuity_parameters = delivery_context_parameters
    writer_recovery_context = (
        "new_session" if fresh_writers else "compact_and_continue_session"
    )
    implementation_recovery_context = (
        "new_session"
        if fresh_writers
        else (
            "continue_session"
            if coder_selection_enabled
            else "compact_and_continue_session"
        )
    )
    non_writer_recovery_context = "compact_and_continue_session"
    final_compliance = (
        pull_requests
        and profile.capability("compliance_review")
    )
    package_publish = profile.package_publish_after_main()
    merged_target = "publish_package" if package_publish else "cleanup"
    merged_prompt = (
        package_publish_prompt(profile)
        if package_publish
        else cleanup_prompt(profile, merged=True)
    )
    selection_carriers = (
        (TASK_SHORT_ID, ACCEPTED_PLAN_SHA256)
        if coder_selection_enabled
        else ()
    )
    implementation_parameters = (
        (WORKSPACE, PLAN, WORK_KIND)
        + selection_carriers
        + ((REVIEW_CONTEXT,) if coder_selection_enabled else ())
        + delivery_context_parameters
    )
    branch_identity_parameters = (
        implementation_parameters
        + (
            (PLAN_ROUTE, PLAN_ROUTE_CONTEXT)
            if coder_selection_enabled
            else ()
        )
    )
    branch_identity_enabled = profile.branch_identity_policy() != "task"
    nodes: list[NodeSpec] = [
        NodeSpec("backlog", "start", "Backlog"),
        agent_node("plan", "Plan", planner),
        agent_node("plan_review", "Independent Plan Review", plan_review_role),
        NodeSpec(
            "plan_contract",
            "script",
            "Plan Contract Accept",
            script_path=profile.command("plan_contract_accept"),
        ),
        NodeSpec(
            "plan_contract_continue",
            "script",
            "Plan Contract Continue Guard",
            script_path=profile.command("plan_contract_continue"),
        ),
        NodeSpec(
            "plan_contract_verify",
            "script",
            "Plan Contract Verify Guard",
            script_path=profile.command("plan_contract_verify"),
        ),
        agent_node(
            "plan_revalidation",
            "Plan Revalidation",
            planner,
        ),
        agent_node("implement", "Implement", implementation),
        NodeSpec(
            "verification_dispatch",
            "script",
            "Verification Dispatch",
            script_path=profile.command("dispatch"),
        ),
        NodeSpec(
            "deterministic_verify",
            "script",
            "Deterministic Verify",
            script_path=profile.command("verify"),
        ),
        agent_node("verification_gate", "Verification Gate", gate),
        agent_node("fix", "Fix", fix),
        agent_node("cleanup", "Cleanup", release),
        NodeSpec("wont_do", "terminal", "Won't Do"),
        NodeSpec("done", "terminal", "Done"),
    ]
    if branch_identity_enabled:
        nodes.extend(
            [
                NodeSpec(
                    "branch_identity",
                    "script",
                    "Branch Identity",
                    script_path=profile.command("branch_identity"),
                ),
                agent_node(
                    "branch_identity_resolution",
                    "Branch Identity Resolution",
                    orchestrator,
                ),
            ]
        )
    if fresh_writers:
        nodes.append(
            NodeSpec(
                "plan_contract_fix_continue",
                "script",
                "Plan Contract Fix Guard",
                script_path=profile.command("plan_contract_fix_continue"),
            )
        )

    review_branches = ["deterministic_verify"]
    if profile.capability("standards_review"):
        nodes.append(
            agent_node(
                "standards_review",
                "Standards Review",
                profile.role("standards_review"),
            )
        )
        review_branches.append("standards_review")
    if profile.capability("spec_review"):
        nodes.append(
            agent_node(
                "spec_review",
                "Spec Review",
                profile.role("spec_review"),
            )
        )
        review_branches.append("spec_review")
    if len(review_branches) > 1:
        nodes.append(NodeSpec("verification_join", "join", "Verification Join"))

    if smoke_enabled:
        nodes.append(agent_node("smoke", "Smoke Test", qa))
    if pull_requests:
        if final_compliance:
            nodes.append(
                agent_node(
                    "compliance",
                    "Compliance Review",
                    profile.role("compliance"),
                )
            )
            nodes.append(
                agent_node(
                    "evidence_repair",
                    "Evidence Repair",
                    fix,
                )
            )
        nodes.extend(
            [
                agent_node("prepare_pr", "Prepare PR", release),
                agent_node("waiting_pr", "Waiting PR", ci),
                NodeSpec(
                    "merge_watch",
                    "script",
                    "Wait For PR Merge",
                    script_path=profile.command("wait_pr"),
                ),
            ]
        )
        if package_publish:
            nodes.append(
                agent_node(
                    "publish_package",
                    "Publish Package",
                    profile.role("package_release"),
                )
            )
    if profile.capability("ci_monitoring"):
        if source_ci:
            nodes.append(
                NodeSpec(
                    "ci_prepare",
                    "script",
                    "Prepare CI Contract",
                    script_path=profile.command("prepare_ci"),
                )
            )
        nodes.append(
            NodeSpec(
                "ci_watch",
                "script",
                "CI Watch",
                script_path=profile.command("wait_ci"),
            )
        )
        nodes.append(agent_node("ci_monitor", "Monitor CI", ci))
    if profile.capability("managed_worktrees"):
        nodes.append(
            NodeSpec(
                "task_janitor",
                "script",
                "Task Janitor",
                script_path=profile.command("janitor"),
            )
        )

    edges: list[EdgeSpec] = [
        EdgeSpec(
            key="start_plan",
            source="backlog",
            transition="start",
            target="plan",
            prompt=plan_prompt(
                profile,
                recovery_aware=fresh_writers,
                coder_selection=coder_selection_enabled,
            ),
            transition_description="Start one planning session for this task.",
        ),
    ]
    edges.extend(
        [
            EdgeSpec(
                key="plan_review",
                source="plan",
                transition="review_plan",
                target="plan_review",
                prompt=plan_review_prompt(
                    profile,
                    coder_selection=coder_selection_enabled,
                ),
                transition_description=(
                    "Planning is complete; independently review its authority, "
                    "scope, evidence, and executable ordering."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
            EdgeSpec(
                key="plan_review_accept",
                source="plan_review",
                transition="accepted",
                target="plan_contract",
                transition_description=(
                    "The independent plan review passed; accept the normalized "
                    "plan contract and continue through its declared route."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_REVIEW_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
            EdgeSpec(
                key="plan_review_revalidate",
                source="plan_review",
                transition="needs_changes",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(
                    profile,
                    from_review=True,
                    coder_selection=coder_selection_enabled,
                ),
                transition_description=(
                    "The read-only review found plan-contract defects; revise "
                    "the plan before any writer or verification stage proceeds."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_REVIEW_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
            recovery_edge(
                "plan_review",
                context=non_writer_recovery_context,
                carry_delivery_context=delivery_context_enabled,
                extra_parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
            cancellation_edge("plan_review"),
            EdgeSpec(
                key="plan_review_resume_continue",
                source="plan_review",
                transition="resume_continue",
                target="plan_contract_continue",
                transition_description=(
                    "The previously accepted plan, approval, and independent "
                    "review still apply; guard the unchanged contract before "
                    "resuming writer work."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + delivery_context_parameters,
            ),
            EdgeSpec(
                key="plan_review_resume_verify",
                source="plan_review",
                transition="resume_verify",
                target="plan_contract_verify",
                transition_description=(
                    "The previously accepted plan, approval, and independent "
                    "review still apply; guard the unchanged contract before "
                    "verification."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + delivery_context_parameters,
            ),
            EdgeSpec(
                key="plan_revalidation_review",
                source="plan_revalidation",
                transition="review_plan",
                target="plan_review",
                context="continue_session",
                context_source="previous_target",
                prompt=plan_review_prompt(
                    profile,
                    coder_selection=coder_selection_enabled,
                ),
                transition_description=(
                    "The plan contract was reconciled; independently re-review "
                    "it before accepting the new snapshot."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
            recovery_edge(
                "plan_revalidation",
                context=non_writer_recovery_context,
                carry_delivery_context=delivery_context_enabled,
                extra_parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ),
            ),
            cancellation_edge("plan_revalidation"),
            EdgeSpec(
                key="plan_revalidation_resume_continue",
                source="plan_revalidation",
                transition="resume_continue",
                target="plan_contract_continue",
                transition_description=(
                    "No new finding or material change invalidates the prior "
                    "accepted review; guard the unchanged contract before "
                    "resuming writer work."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + delivery_context_parameters,
            ),
            EdgeSpec(
                key="plan_revalidation_resume_verify",
                source="plan_revalidation",
                transition="resume_verify",
                target="plan_contract_verify",
                transition_description=(
                    "No new finding or material change invalidates the prior "
                    "accepted review; guard the unchanged contract before "
                    "verification."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + delivery_context_parameters,
            ),
            EdgeSpec(
                key="plan_contract_continue_revalidate",
                source="plan_contract_continue",
                transition="changed",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(
                    profile,
                    from_review=False,
                    coder_selection=coder_selection_enabled,
                ),
                transition_description=(
                    "The normalized accepted plan changed materially; reconcile "
                    "authority and acceptance before continuing."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_CHANGE_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
            EdgeSpec(
                key="plan_contract_verify_revalidate",
                source="plan_contract_verify",
                transition="changed",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(
                    profile,
                    from_review=False,
                    coder_selection=coder_selection_enabled,
                ),
                transition_description=(
                    "The normalized accepted plan changed materially; reconcile "
                    "authority and acceptance before verification."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_CHANGE_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            ),
        ]
    )
    if fresh_writers:
        edges.extend(
            [
                EdgeSpec(
                    key="plan_review_resume_fix_continue",
                    source="plan_review",
                    transition="resume_fix_continue",
                    target="plan_contract_fix_continue",
                    transition_description=(
                        "The previously accepted plan, approval, and review "
                        "still apply; guard the exact remaining bounded Fix "
                        "bundle before resuming it."
                    ),
                    parameters=(
                        WORKSPACE,
                        FIX_CONTEXT,
                        TASK_SHORT_ID,
                    ) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="plan_revalidation_resume_fix_continue",
                    source="plan_revalidation",
                    transition="resume_fix_continue",
                    target="plan_contract_fix_continue",
                    transition_description=(
                        "No new finding or material change invalidates the "
                        "prior accepted review; guard the exact remaining "
                        "bounded Fix bundle before resuming it."
                    ),
                    parameters=(
                        WORKSPACE,
                        FIX_CONTEXT,
                        TASK_SHORT_ID,
                    ) + fix_continuity_parameters,
                ),
            ]
        )
    if fresh_writers:
        edges.append(
            EdgeSpec(
                key="plan_contract_fix_revalidate",
                source="plan_contract_fix_continue",
                transition="changed",
                target="plan_revalidation",
                context="continue_session",
                context_source="node:plan",
                prompt=plan_revalidation_prompt(
                    profile,
                    from_review=False,
                    coder_selection=coder_selection_enabled,
                ),
                transition_description=(
                    "A bounded Fix slice changed the accepted plan; reconcile "
                    "authority before continuing repair."
                ),
                parameters=(
                    WORKSPACE,
                    PLAN,
                    WORK_KIND,
                    PLAN_ROUTE,
                    PLAN_ROUTE_CONTEXT,
                    PLAN_CHANGE_REPORT,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                    *delivery_context_parameters,
                ),
            )
        )
    if branch_identity_enabled:
        edges.extend(
            [
                EdgeSpec(
                    key="plan_contract_branch_identity",
                    source="plan_contract",
                    transition="start",
                    target="branch_identity",
                    transition_description=(
                        "The accepted plan contract is stable; resolve "
                        "source-control branch identity before implementation."
                    ),
                    parameters=implementation_parameters,
                ),
                EdgeSpec(
                    key="branch_identity_implement",
                    source="branch_identity",
                    transition="ready",
                    target="implement",
                    context=writer_handoff_context,
                    context_source=branch_identity_handoff_source,
                    prompt=implement_prompt(
                        profile,
                        fresh_session=fresh_writers,
                    ),
                    transition_description=(
                        "Branch identity is deterministic and implementation "
                        "can start."
                    ),
                    parameters=implementation_parameters,
                ),
                EdgeSpec(
                    key="branch_identity_resolution",
                    source="branch_identity",
                    transition="blocked",
                    target="branch_identity_resolution",
                    prompt=branch_identity_resolution_prompt(
                        profile,
                        coder_selection=coder_selection_enabled,
                    ),
                    transition_description=(
                        "Branch identity is ambiguous or collides with existing "
                        "repository state and requires a user decision."
                    ),
                    parameters=(BLOCKER,) + implementation_parameters,
                ),
                EdgeSpec(
                    key="branch_identity_retry",
                    source="branch_identity_resolution",
                    transition="retry",
                    target="branch_identity",
                    transition_description=(
                        "Retry deterministic branch identity after the reported "
                        "collision or repository blocker is resolved."
                    ),
                    parameters=implementation_parameters,
                ),
                recovery_edge(
                    "branch_identity_resolution",
                    context=non_writer_recovery_context,
                    carry_delivery_context=delivery_context_enabled,
                    extra_parameters=implementation_parameters,
                    extra_prompt=(
                        "When the exact branch blocker is resolved, choose "
                        "`retry`. Do not rename, delete, or overwrite another "
                        "branch from this agent node."
                    ),
                ),
                cancellation_edge("branch_identity_resolution"),
            ]
        )
    else:
        edges.append(
            EdgeSpec(
                key="plan_contract_implement",
                source="plan_contract",
                transition="start",
                target="implement",
                context=writer_handoff_context,
                context_source=branch_identity_handoff_source,
                prompt=implement_prompt(profile, fresh_session=fresh_writers),
                transition_description=(
                    "The accepted plan contract is stable and implementation "
                    "can start without branch-identity routing."
                ),
                parameters=implementation_parameters,
            )
        )
    edges.extend(
        [
            EdgeSpec(
                key="plan_contract_continue_implement",
                source="plan_contract",
                transition="continue",
                target="implement",
                context=implementation_continuation_context,
                context_source=plan_contract_continuation_source,
                prompt=implement_prompt(profile, fresh_session=fresh_writers),
                transition_description=(
                    "The accepted plan contract is unchanged; continue with "
                    "the next ready writer-owned step."
                ),
                parameters=implementation_parameters,
            ),
            EdgeSpec(
                key="plan_contract_verify",
                source="plan_contract",
                transition="verify",
                target="verification_dispatch",
                transition_description=(
                    "The accepted plan contract is unchanged and writer work "
                    "is complete; normalize inputs for read-only verification."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                ) + fix_continuity_parameters,
            ),
            EdgeSpec(
                key="plan_contract_checked_continue",
                source="plan_contract_continue",
                transition="stable",
                target="implement",
                context=implementation_continuation_context,
                context_source=plan_contract_continuation_source,
                prompt=implement_prompt(profile, fresh_session=fresh_writers),
                transition_description=(
                    "The accepted plan contract is unchanged; continue with "
                    "the next ready writer-owned step."
                ),
                parameters=implementation_parameters,
            ),
            EdgeSpec(
                key="plan_contract_checked_verify",
                source="plan_contract_verify",
                transition="stable",
                target="verification_dispatch",
                transition_description=(
                    "The accepted plan contract is unchanged and writer work "
                    "is complete; normalize inputs for read-only verification."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                ) + fix_continuity_parameters,
            ),
        ]
    )
    if fresh_writers:
        edges.extend(
            [
                EdgeSpec(
                    key="plan_contract_fix_continue",
                    source="plan_contract",
                    transition="fix_continue",
                    target="fix",
                    context="new_session",
                    prompt=fix_prompt(profile, bounded=True),
                    transition_description=(
                        "The independently revalidated plan is accepted; "
                        "continue the remaining bounded repair bundle."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="plan_contract_checked_fix",
                    source="plan_contract_fix_continue",
                    transition="stable",
                    target="fix",
                    context="new_session",
                    prompt=fix_prompt(profile, bounded=True),
                    transition_description=(
                        "The accepted plan contract is unchanged; continue only "
                        "the remaining bounded repair bundle."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
                ),
            ]
        )
    edges.extend(
        [
            recovery_edge(
                "plan",
                context=non_writer_recovery_context,
                carry_delivery_context=delivery_context_enabled,
            ),
            cancellation_edge("plan"),
        ]
    )
    edges.extend(
        [
            EdgeSpec(
                key="implement_continue",
                source="implement",
                transition="continue_implementation",
                target="plan_contract_continue",
                transition_description=(
                    "One plan step is complete; check the accepted plan contract "
                    "before starting the next writer slice."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + delivery_context_parameters,
            ),
            EdgeSpec(
                key="implement_verify",
                source="implement",
                transition="verify",
                target="plan_contract_verify",
                transition_description=(
                    "Implementation is complete; check the accepted plan "
                    "contract before read-only verification."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + delivery_context_parameters,
            ),
            recovery_edge(
                "implement",
                profile=profile,
                context=implementation_recovery_context,
                fresh_session=fresh_writers,
                carry_delivery_context=delivery_context_enabled,
                extra_parameters=(WORK_KIND,),
                extra_prompt=work_kind_recovery_instruction(profile),
            ),
            cancellation_edge("implement"),
            EdgeSpec(
                key="fix_verify",
                source="fix",
                transition="verify",
                target="plan_contract_verify",
                transition_description=(
                    "Task-scoped fixes are complete; check the accepted plan "
                    "contract before rerunning every verification branch."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    TASK_SHORT_ID,
                ) + fix_continuity_parameters,
            ),
            recovery_edge(
                "fix",
                profile=profile,
                context=writer_recovery_context,
                fresh_session=fresh_writers,
                carry_delivery_context=delivery_context_enabled,
                extra_parameters=fix_continuity_parameters,
                extra_prompt=(
                    "Preserve the exact PR identity, CI contract/report, and "
                    "acknowledged feedback cursor through Fix recovery."
                    if fix_continuity_parameters
                    else ""
                ),
            ),
            cancellation_edge("fix"),
        ]
    )

    if fresh_writers:
        edges.append(
            EdgeSpec(
                key="fix_continue",
                source="fix",
                transition="continue_fix",
                target="plan_contract_fix_continue",
                transition_description=(
                    "One bounded fix slice is complete; check the accepted plan "
                    "before continuing the remaining task-scoped findings."
                ),
                parameters=(
                    WORKSPACE,
                    FIX_CONTEXT,
                    TASK_SHORT_ID,
                ) + fix_continuity_parameters,
            )
        )

    fanout_parameters = (WORKSPACE, REVIEW_CONTEXT) + fix_continuity_parameters
    edges.append(
        EdgeSpec(
            key="dispatch_deterministic_verify",
            source="verification_dispatch",
            transition="fanout_verify",
            target="deterministic_verify",
            transition_description=(
                "Run deterministic verification and independent read-only reviews."
            ),
            parameters=fanout_parameters,
        )
    )
    edges.append(
        EdgeSpec(
            key="dispatch_invalid_workspace",
            source="verification_dispatch",
            transition="invalid_workspace",
            target="fix",
            context="new_session",
            context_source="immediate_source",
            prompt=workspace_path_fix_prompt(profile),
            transition_description=(
                "Reject an artifact or foreign workspace path before verification."
            ),
            parameters=(
                (WORKSPACE,)
                if profile.delivery_reliability_policy() == "v1"
                else ()
            ) + (
                REPORTED_WORKSPACE,
                FIX_CONTEXT,
            ) + fix_continuity_parameters,
        )
    )
    if "standards_review" in review_branches:
        edges.append(
            EdgeSpec(
                key="dispatch_standards_review",
                source="verification_dispatch",
                transition="fanout_verify",
                target="standards_review",
                prompt=standards_review_prompt(profile),
                transition_description=(
                    "Run deterministic verification and independent read-only reviews."
                ),
                parameters=fanout_parameters,
            )
        )
    if "spec_review" in review_branches:
        edges.append(
            EdgeSpec(
                key="dispatch_spec_review",
                source="verification_dispatch",
                transition="fanout_verify",
                target="spec_review",
                prompt=spec_review_prompt(profile),
                transition_description=(
                    "Run deterministic verification and independent read-only reviews."
                ),
                parameters=fanout_parameters,
            )
        )

    gate_prompt = verification_gate_prompt(profile)
    if len(review_branches) > 1:
        edges.extend(
            [
                EdgeSpec(
                    key="deterministic_report_join",
                    source="deterministic_verify",
                    transition="reported",
                    target="verification_join",
                    transition_description=(
                        "Deterministic verification completed and reported its status."
                    ),
                    parameters=(
                        VERIFICATION_STATUS,
                        VERIFICATION_REPORT,
                    ),
                ),
                EdgeSpec(
                    key="verification_join_gate",
                    source="verification_join",
                    transition="evaluate",
                    target="verification_gate",
                    prompt=gate_prompt,
                    transition_description=(
                        "All direct verification branches reported; evaluate them."
                    ),
                ),
            ]
        )
        if "standards_review" in review_branches:
            edges.append(
                EdgeSpec(
                    key="standards_report_join",
                    source="standards_review",
                    transition="reported",
                    target="verification_join",
                    transition_description=(
                        "Repository standards review completed and reported its status."
                    ),
                    parameters=(
                        STANDARDS_STATUS,
                        STANDARDS_REPORT,
                    ),
                )
            )
        if "spec_review" in review_branches:
            edges.append(
                EdgeSpec(
                    key="spec_report_join",
                    source="spec_review",
                    transition="reported",
                    target="verification_join",
                    transition_description=(
                        "Specification review completed and reported its status."
                    ),
                    parameters=(
                        SPEC_STATUS,
                        REVIEW_REPORT,
                    ),
                )
            )
    else:
        edges.append(
            EdgeSpec(
                key="deterministic_report_gate",
                source="deterministic_verify",
                transition="reported",
                target="verification_gate",
                prompt=gate_prompt,
                transition_description=(
                    "Deterministic verification completed; evaluate its report."
                ),
                parameters=(
                    VERIFICATION_STATUS,
                    VERIFICATION_REPORT,
                ),
            )
        )

    edges.extend(
        [
            EdgeSpec(
                key="gate_fix",
                source="verification_gate",
                transition="needs_changes",
                target="fix",
                context=fix_context,
                context_source=fix_context_source,
                prompt=fix_prompt(profile, bounded=fresh_writers),
                transition_description=(
                    "Verification found task-scoped issues for the single writer to fix."
                ),
                parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
            ),
            EdgeSpec(
                key="gate_reverify_after_user_action",
                source="verification_gate",
                transition="needs_user_action",
                target="verification_dispatch",
                requires_approval=True,
                transition_description=(
                    "Verification is externally blocked; rerun all branches after approval."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    BLOCKER,
                ) + fix_continuity_parameters,
            ),
            EdgeSpec(
                key="gate_retry_verification",
                source="verification_gate",
                transition="retry_verification",
                target="verification_dispatch",
                context="new_session",
                context_source="immediate_source",
                requires_approval=False,
                transition_description=(
                    "A capable owner resolved and settled a technical blocker; "
                    "run fresh verification branches."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                ) + delivery_context_parameters,
            ),
            cancellation_edge("verification_gate"),
        ]
    )

    smoke_policy = profile.smoke_policy()
    delivery_target = post_smoke_target(profile)
    if smoke_policy in {"conditional", "required"}:
        edges.append(
            EdgeSpec(
                key="gate_smoke_required",
                source="verification_gate",
                transition="smoke_required",
                target="smoke",
                prompt=smoke_prompt(profile),
                transition_description=(
                    "All verification passed and runtime smoke is required."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    SMOKE_RATIONALE,
                    SMOKE_SCOPE,
                ) + fix_continuity_parameters,
            )
        )
    if smoke_policy in {"conditional", "disabled"}:
        edges.append(
            EdgeSpec(
                key="gate_delivery_ready",
                source="verification_gate",
                transition="delivery_ready",
                target=delivery_target,
                prompt=delivery_prompt(profile, delivery_target),
                transition_description=(
                    "All verification passed and delivery may continue without "
                    "runtime smoke."
                ),
                parameters=(
                    WORKSPACE,
                    REVIEW_CONTEXT,
                    SMOKE_RATIONALE,
                ) + fix_continuity_parameters,
            )
        )

    if smoke_policy != "disabled":
        smoke_delivery_edge_key = (
            "smoke_prepare_pr"
            if profile.capability("pull_requests")
            else f"smoke_{delivery_target}"
        )
        edges.extend(
            [
                EdgeSpec(
                    # Keep the historical key stable while taskless Delivery
                    # workflows are reconciled from Prepare PR to Compliance.
                    key=smoke_delivery_edge_key,
                    source="smoke",
                    transition="passed",
                    target=delivery_target,
                    prompt=delivery_prompt(profile, delivery_target),
                    transition_description=(
                        "Focused smoke testing passed and delivery may continue."
                    ),
                    parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                    ) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="smoke_fix",
                    source="smoke",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=fix_prompt(profile, bounded=fresh_writers),
                    transition_description=(
                        "Smoke testing found task-scoped implementation issues."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
                ),
                recovery_edge(
                    "smoke",
                    context=non_writer_recovery_context,
                    carry_delivery_context=delivery_context_enabled,
                    extra_prompt=smoke_recovery_settlement_instruction(),
                ),
                cancellation_edge("smoke"),
            ]
        )

    if final_compliance:
        edges.extend(
            [
                EdgeSpec(
                    key="compliance_prepare_pr",
                    source="compliance",
                    transition="ship_pr",
                    target="prepare_pr",
                    prompt=prepare_pr_prompt(profile),
                    transition_description=(
                        "Final compliance passed; prepare or update the task pull request."
                    ),
                    parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                        COMPLIANCE_REPORT,
                    ) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="compliance_fix",
                    source="compliance",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=fix_prompt(profile, bounded=fresh_writers),
                    transition_description=(
                        "Final compliance found task-scoped issues that require fixes."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="compliance_evidence_repair",
                    source="compliance",
                    transition="repair_evidence",
                    target="evidence_repair",
                    context="new_session",
                    prompt=evidence_repair_prompt(profile),
                    transition_description=(
                        "Only packaging evidence is incomplete; repair those "
                        "artifacts without repeating source or runtime work."
                    ),
                    parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                        EVIDENCE_CONTEXT,
                    ) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="compliance_needs_user_action",
                    source="compliance",
                    transition="needs_user_action",
                    target="compliance",
                    context=non_writer_recovery_context,
                    prompt=compliance_recovery_prompt(profile),
                    requires_approval=True,
                    transition_description=(
                        "Final compliance is externally blocked; recheck after approval."
                    ),
                    parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                        BLOCKER,
                    ) + fix_continuity_parameters,
                ),
                cancellation_edge("compliance"),
                EdgeSpec(
                    key="evidence_repair_compliance",
                    source="evidence_repair",
                    transition="recheck_compliance",
                    target="compliance",
                    context="compact_and_continue_session",
                    context_source="previous_target",
                    prompt=compliance_recheck_prompt(profile),
                    transition_description=(
                        "Packaging evidence was repaired; rerun only final "
                        "Compliance Review."
                    ),
                    parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                    ) + fix_continuity_parameters,
                ),
                EdgeSpec(
                    key="evidence_repair_fix",
                    source="evidence_repair",
                    transition="needs_source_fix",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=fix_prompt(profile, bounded=fresh_writers),
                    transition_description=(
                        "Evidence repair proved that substantive source work "
                        "is still required."
                    ),
                    parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
                ),
                recovery_edge(
                    "evidence_repair",
                    profile=profile,
                    context=writer_recovery_context,
                    fresh_session=fresh_writers,
                    carry_delivery_context=delivery_context_enabled,
                    extra_parameters=(
                        WORKSPACE,
                        REVIEW_CONTEXT,
                        EVIDENCE_CONTEXT,
                    ),
                    extra_prompt=(
                        "Preserve the exact workspace, review context, and "
                        "packaging-only evidence scope. Do not broaden repair."
                    ),
                ),
                cancellation_edge("evidence_repair"),
            ]
        )

    if profile.capability("pull_requests"):
        created_target = (
            "ci_watch" if profile.capability("ci_monitoring") else "waiting_pr"
        )
        ci_entry_target = "ci_prepare" if source_ci else created_target
        if source_ci:
            prepare_pr_parameters = (
                WORKSPACE,
                TASK_SHORT_ID,
            ) + delivery_context_parameters
        else:
            prepare_pr_parameters = (
                WORKSPACE,
                PR_URL,
                BRANCH_NAME,
                MERGE_STRATEGY,
            ) + pr_cursor_parameters + ci_contract_parameters
        if source_ci:
            ci_prepare_retry_parameters = (
                WORKSPACE,
                TASK_SHORT_ID,
            ) + delivery_context_parameters
        else:
            ci_prepare_retry_parameters = (
                WORKSPACE,
                PR_URL,
                BRANCH_NAME,
                MERGE_STRATEGY,
                *ci_contract_retry_parameters,
            ) + pr_cursor_parameters
        if source_ci:
            ci_monitor_watch_parameters = (
                WORKSPACE,
                TASK_SHORT_ID,
            ) + delivery_context_parameters
        else:
            ci_monitor_watch_parameters = (
                WORKSPACE,
                PR_URL,
                BRANCH_NAME,
                MERGE_STRATEGY,
                *ci_contract_parameters,
                CI_REPORT,
            ) + pr_cursor_parameters
        waiting_pr_fix_parameters = (
            (WORKSPACE, PR_REPORT) + delivery_context_parameters
            if delivery_context_enabled
            else (
                (WORKSPACE,)
                + (MERGE_STRATEGY, PR_REPORT)
                + ci_contract_retry_parameters
                + pr_cursor_parameters
            )
        )
        edges.extend(
            [
                EdgeSpec(
                    key=f"prepare_pr_{ci_entry_target}",
                    source="prepare_pr",
                    transition="monitor_ci",
                    target=ci_entry_target,
                    prompt=(
                        None
                        if ci_entry_target in {"ci_watch", "ci_prepare"}
                        else waiting_pr_prompt(profile)
                    ),
                    transition_description=(
                        "The pull request exists and its delivery state must be checked."
                    ),
                    parameters=prepare_pr_parameters,
                ),
                EdgeSpec(
                    key="prepare_pr_no_pr",
                    source="prepare_pr",
                    transition="no_pr",
                    target="cleanup",
                    prompt=cleanup_prompt(profile, no_pr=True),
                    requires_approval=True,
                    transition_description=(
                        "A pull request is not applicable; require approval before cleanup."
                    ),
                    parameters=(PR_REPORT,),
                ),
                EdgeSpec(
                    key="prepare_pr_fix",
                    source="prepare_pr",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=pr_recovery_fix_prompt(
                        profile,
                        bounded=fresh_writers,
                    ),
                    transition_description=(
                        "PR preparation found task-scoped changes that must be fixed."
                    ),
                    parameters=(WORKSPACE, BLOCKER) + delivery_context_parameters,
                ),
                recovery_edge(
                    "prepare_pr",
                    context=non_writer_recovery_context,
                    carry_delivery_context=delivery_context_enabled,
                ),
            ]
        )

        if profile.capability("ci_monitoring"):
            if source_ci:
                edges.extend(
                    [
                        EdgeSpec(
                            key="ci_prepare_ready_initial",
                            source="ci_prepare",
                            transition="ready_initial",
                            target="ci_watch",
                            transition_description=(
                                "The initial CI contract is complete; watch this "
                                "exact PR identity cycle."
                            ),
                            parameters=(
                                WORKSPACE,
                                PR_URL,
                                BRANCH_NAME,
                                MERGE_STRATEGY,
                                *ci_contract_parameters,
                            ) + pr_cursor_parameters,
                        ),
                        EdgeSpec(
                            key="ci_prepare_ready_retry",
                            source="ci_prepare",
                            transition="ready_retry",
                            target="ci_watch",
                            transition_description=(
                                "The unchanged CI contract is ready for another "
                                "deterministic observation."
                            ),
                            parameters=(
                                WORKSPACE,
                                PR_URL,
                                BRANCH_NAME,
                                MERGE_STRATEGY,
                                *ci_contract_retry_parameters,
                            ) + pr_cursor_parameters,
                        ),
                        EdgeSpec(
                            key="ci_prepare_failed",
                            source="ci_prepare",
                            transition="failed",
                            target="ci_monitor",
                            prompt=(
                                ci_preparation_failure_prompt(profile)
                                if source_ci_v3 else ci_prompt(profile)
                            ),
                            transition_description=(
                                "CI preparation could not establish or validate "
                                "the current PR identity and CI cycle contract."
                            ),
                            parameters=(
                                (
                                    WORKSPACE,
                                    TASK_SHORT_ID,
                                    CI_PREPARATION_DIAGNOSTIC,
                                )
                                if source_ci_v3
                                else (
                                    WORKSPACE,
                                    PR_URL,
                                    BRANCH_NAME,
                                    MERGE_STRATEGY,
                                    *ci_contract_parameters,
                                    CI_REPORT,
                                ) + pr_cursor_parameters
                            ),
                        ),
                        EdgeSpec(
                            key="ci_prepare_merged",
                            source="ci_prepare",
                            transition="pr_merged",
                            target=merged_target,
                            prompt=merged_prompt,
                            requires_approval=package_publish,
                            transition_description=(
                                "The pull request merged during CI preparation."
                            ),
                            parameters=(
                                WORKSPACE,
                                PR_URL,
                                BRANCH_NAME,
                                MERGE_REPORT,
                            ),
                        ),
                    ]
                )
            edges.extend(
                [
                    EdgeSpec(
                        key="ci_watch_waiting_pr",
                        source="ci_watch",
                        transition="passed",
                        target="waiting_pr",
                        prompt=waiting_pr_prompt(profile),
                        transition_description=(
                            "The deterministic watcher confirmed terminal "
                            "green CI; classify merge readiness once."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            *ci_contract_parameters,
                            CI_REPORT,
                        ) + pr_cursor_parameters,
                    ),
                    EdgeSpec(
                        key="ci_watch_state_changed",
                        source="ci_watch",
                        transition="state_changed",
                        target="waiting_pr",
                        prompt=waiting_pr_changed_prompt(profile),
                        transition_description=(
                            "The PR feedback, mergeability, or identity changed "
                            "while CI was still being observed; classify the "
                            "material change before passive waiting resumes."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            PR_REPORT,
                            PR_HEAD_OID,
                            PR_BASE_OID,
                            *ci_contract_parameters,
                            CI_REPORT,
                        ) + pr_cursor_parameters,
                    ),
                    EdgeSpec(
                        key="ci_watch_diagnose",
                        source="ci_watch",
                        transition="failed",
                        target="ci_monitor",
                        prompt=ci_prompt(profile),
                        transition_description=(
                            "CI reached a failed, cancelled, empty, or "
                            "infrastructure-error state; classify it once."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            *ci_contract_parameters,
                            CI_REPORT,
                        ) + pr_cursor_parameters,
                    ),
                    *(
                        [
                            EdgeSpec(
                                key="ci_watch_source_changed",
                                source="ci_watch",
                                transition="source_changed",
                                target="ci_prepare",
                                transition_description=(
                                    "The PR identity changed during observation; "
                                    "start a fresh CI cycle for its current head and base."
                                ),
                                parameters=(
                                    WORKSPACE,
                                    PR_URL,
                                    BRANCH_NAME,
                                    MERGE_STRATEGY,
                                    *ci_contract_parameters,
                                    CI_REPORT,
                                ) + pr_cursor_parameters,
                            )
                        ]
                        if source_ci
                        else []
                    ),
                    EdgeSpec(
                        key="ci_watch_merged",
                        source="ci_watch",
                        transition="pr_merged",
                        target=merged_target,
                        prompt=merged_prompt,
                        requires_approval=package_publish,
                        transition_description=(
                            "The pull request merged while deterministic CI "
                            "watching was active."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_monitor_watch",
                        source="ci_monitor",
                        transition="watch_ci",
                        target="ci_prepare" if source_ci else "ci_watch",
                        transition_description=(
                            "A bounded infrastructure retry or refreshed CI "
                            "run is ready for deterministic preparation."
                        ),
                        parameters=ci_monitor_watch_parameters,
                    ),
                    *(
                        [
                            EdgeSpec(
                                key="ci_monitor_retry_preparation",
                                source="ci_monitor",
                                transition="retry_preparation",
                                target="ci_prepare",
                                transition_description=(
                                    "A typed preparation diagnostic permits a "
                                    "fresh deterministic retry with its original "
                                    "validated PR and CI context."
                                ),
                                parameters=(
                                    WORKSPACE,
                                    TASK_SHORT_ID,
                                    *delivery_context_parameters,
                                ),
                            ),
                            EdgeSpec(
                                key="ci_monitor_preparation_needs_user_action",
                                source="ci_monitor",
                                transition="preparation_needs_user_action",
                                target="ci_monitor",
                                context=non_writer_recovery_context,
                                prompt=ci_preparation_user_action_prompt(profile),
                                requires_approval=True,
                                transition_description=(
                                    "The preparation diagnostic requires an "
                                    "actual human decision or external recovery."
                                ),
                                parameters=(
                                    WORKSPACE,
                                    TASK_SHORT_ID,
                                    CI_PREPARATION_DIAGNOSTIC,
                                    BLOCKER,
                                ),
                            ),
                        ]
                        if source_ci_v3
                        else []
                    ),
                    EdgeSpec(
                        key="ci_monitor_merged",
                        source="ci_monitor",
                        transition="pr_merged",
                        target=merged_target,
                        prompt=merged_prompt,
                        requires_approval=package_publish,
                        transition_description=(
                            "The pull request merged before CI observation "
                            "completed; continue from the confirmed merged state."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="ci_monitor_fix",
                        source="ci_monitor",
                        transition="needs_changes",
                        target="fix",
                        context=fix_context,
                        context_source=fix_context_source,
                        prompt=fix_prompt(profile, bounded=fresh_writers),
                        transition_description=(
                            "CI found task-scoped failures that require fixes."
                        ),
                        parameters=(WORKSPACE, FIX_CONTEXT) + fix_continuity_parameters,
                    ),
                    recovery_edge(
                        "ci_monitor",
                        context=non_writer_recovery_context,
                        extra_parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            *ci_contract_parameters,
                            CI_REPORT,
                        ) + pr_cursor_parameters,
                        extra_prompt=(
                            "Preserve the exact PR, branch, merge strategy, "
                            "and terminal CI report."
                        ),
                    ),
                ]
            )
            if not source_ci:
                edges.append(
                    EdgeSpec(
                        key="ci_monitor_waiting_pr",
                        source="ci_monitor",
                        transition="waiting_pr",
                        target="waiting_pr",
                        prompt=waiting_pr_prompt(profile),
                        transition_description=(
                            "Required CI checks passed; wait for an actual merge."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_STRATEGY,
                            *ci_contract_parameters,
                            CI_REPORT,
                        ) + pr_cursor_parameters,
                    )
                )

        edges.extend(
            [
                EdgeSpec(
                    key="fix_pr_merged_cleanup",
                    source="fix",
                    transition="pr_merged",
                    target=merged_target,
                    context="new_session",
                    prompt=merged_prompt,
                    requires_approval=package_publish,
                    transition_description=(
                        "Recovery only: the pull request is already merged; "
                        "skip obsolete Fix work and continue post-merge delivery."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_cleanup",
                    source="waiting_pr",
                    transition="pr_merged",
                    target=merged_target,
                    prompt=merged_prompt,
                    requires_approval=package_publish,
                    transition_description=(
                        "The pull request is confirmed merged; continue post-merge delivery."
                    ),
                    parameters=(WORKSPACE, PR_URL, BRANCH_NAME, MERGE_REPORT),
                ),
                EdgeSpec(
                    key="waiting_pr_watch_merge",
                    source="waiting_pr",
                    transition="watch_merge",
                    target="merge_watch",
                    transition_description=(
                        "The pull request is open and feasible; wait "
                        "deterministically for a meaningful state change."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        PR_HEAD_OID,
                        PR_BASE_OID,
                        *ci_contract_retry_parameters,
                    ) + pr_cursor_parameters,
                ),
                EdgeSpec(
                    key="merge_watch_still_waiting",
                    source="merge_watch",
                    transition="still_waiting",
                    target="merge_watch",
                    transition_description=(
                        "No meaningful PR state changed during a bounded watch "
                        "window; continue without an agent turn."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        PR_HEAD_OID,
                        PR_BASE_OID,
                        *ci_contract_retry_parameters,
                    ) + pr_cursor_parameters,
                ),
                EdgeSpec(
                    key="merge_watch_state_changed",
                    source="merge_watch",
                    transition="state_changed",
                    target="waiting_pr",
                    context="compact_and_continue_session",
                    context_source="previous_target",
                    prompt=waiting_pr_changed_prompt(profile),
                    transition_description=(
                        "PR state changed materially; let the retained Waiting "
                        "PR session classify it once."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        PR_REPORT,
                        *ci_contract_retry_parameters,
                    ) + pr_cursor_parameters,
                ),
                EdgeSpec(
                    key="merge_watch_cleanup",
                    source="merge_watch",
                    transition="pr_merged",
                    target=merged_target,
                    prompt=merged_prompt,
                    requires_approval=package_publish,
                    transition_description=(
                        "The deterministic watcher confirmed the PR merged."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        MERGE_REPORT,
                    ) + pr_cursor_parameters,
                ),
                EdgeSpec(
                    key="waiting_pr_needs_user_action",
                    source="waiting_pr",
                    transition="needs_user_action",
                    target="waiting_pr",
                    context=non_writer_recovery_context,
                    prompt=waiting_pr_prompt(profile),
                    requires_approval=True,
                    transition_description=(
                        "Waiting PR requires an actual human decision or "
                        "external access recovery."
                    ),
                    parameters=(
                        WORKSPACE,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_STRATEGY,
                        BLOCKER,
                        *ci_contract_retry_parameters,
                    ) + pr_cursor_parameters,
                ),
                EdgeSpec(
                    key="waiting_pr_fix",
                    source="waiting_pr",
                    transition="needs_changes",
                    target="fix",
                    context=fix_context,
                    context_source=fix_context_source,
                    prompt=pr_feedback_fix_prompt(
                        profile,
                        bounded=fresh_writers,
                    ),
                    transition_description=(
                        "The PR state requires task-scoped changes before merge."
                    ),
                    parameters=waiting_pr_fix_parameters,
                ),
                EdgeSpec(
                    key="waiting_pr_close_without_merge",
                    source="waiting_pr",
                    transition="close_without_merge",
                    target="cleanup",
                    prompt=cleanup_prompt(profile, closed=True),
                    requires_approval=True,
                    transition_description=(
                        "The user explicitly approved closing delivery without merge."
                    ),
                    parameters=(WORKSPACE, PR_REPORT, CLOSURE_REASON),
                ),
            ]
        )
        if profile.capability("ci_monitoring"):
            edges.append(
                EdgeSpec(
                    key="waiting_pr_ci_monitor",
                    source="waiting_pr",
                    transition="ci_required",
                    target="ci_prepare" if source_ci else "ci_watch",
                    transition_description=(
                        "The PR head changed or checks restarted; wait "
                        "deterministically for terminal CI state."
                    ),
                    parameters=ci_prepare_retry_parameters,
                )
            )

        if package_publish:
            edges.extend(
                [
                    EdgeSpec(
                        key="publish_cleanup",
                        source="publish_package",
                        transition="published",
                        target="cleanup",
                        prompt=published_cleanup_prompt(profile),
                        transition_description=(
                            "The approved package is verified remotely; clean "
                            "up the merged task resources."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                            PUBLICATION_REPORT,
                        ),
                    ),
                    EdgeSpec(
                        key="publish_needs_user_action",
                        source="publish_package",
                        transition="needs_user_action",
                        target="publish_package",
                        context="compact_and_continue_session",
                        prompt=package_publish_recovery_prompt(profile),
                        requires_approval=True,
                        transition_description=(
                            "Publication is blocked or partial; retry only "
                            "after explicit approval and a fresh remote-state check."
                        ),
                        parameters=(
                            WORKSPACE,
                            PR_URL,
                            BRANCH_NAME,
                            MERGE_REPORT,
                            BLOCKER,
                        ),
                    ),
                ]
            )

    if profile.capability("managed_worktrees"):
        edges.extend(
            [
                EdgeSpec(
                    key="cleanup_task_janitor",
                    source="cleanup",
                    transition="run_janitor",
                    target="task_janitor",
                    transition_description=(
                        "Cleanup reporting is complete; remove only "
                        "deterministically safe task-owned resources."
                    ),
                    parameters=(
                        WORKSPACE,
                        TASK_SHORT_ID,
                        PR_URL,
                        BRANCH_NAME,
                        MERGE_REPORT,
                        CLEANUP_MODE,
                        CLEANUP_SESSION_ID,
                        CLEANUP_REPORT,
                    ),
                ),
                EdgeSpec(
                    key="task_janitor_done",
                    source="task_janitor",
                    transition="done",
                    target="done",
                    transition_description=(
                        "The janitor removed safe resources or preserved them "
                        "with an explicit cleanup report."
                    ),
                    parameters=(CLEANUP_REPORT,),
                ),
                EdgeSpec(
                    key="task_janitor_blocked",
                    source="task_janitor",
                    transition="blocked",
                    target="cleanup",
                    context="compact_and_continue_session",
                    context_source="previous_target",
                    prompt=janitor_recovery_prompt(profile),
                    transition_description=(
                        "Janitor infrastructure failed after safety checks; "
                        "return to Cleanup without losing its context."
                    ),
                    parameters=(CLEANUP_REPORT, BLOCKER),
                ),
                recovery_edge(
                    "cleanup",
                    context=non_writer_recovery_context,
                    extra_prompt=cleanup_evidence_owner_instruction(profile),
                ),
            ]
        )
    else:
        edges.extend(
            [
                EdgeSpec(
                    key="cleanup_done",
                    source="cleanup",
                    transition="done",
                    target="done",
                    transition_description=(
                        "Delivery and conservative cleanup are complete."
                    ),
                    parameters=(CLEANUP_REPORT,),
                ),
                recovery_edge(
                    "cleanup",
                    context=non_writer_recovery_context,
                    extra_prompt=cleanup_evidence_owner_instruction(profile),
                ),
            ]
        )

    replan_owners = (
        ("implement", "fix", "compliance")
        if profile.delivery_reliability_policy() == "v1"
        else ("fix", "compliance")
    )
    for source in replan_owners:
        if any(node.key == source for node in nodes):
            edges.append(
                EdgeSpec(
                    key=f"{source}_replan",
                    source=source,
                    transition="replan",
                    target="plan_revalidation",
                    context="continue_session",
                    context_source="node:plan",
                    prompt=owner_replan_prompt(profile),
                    transition_description=(
                        "The accepted execution plan needs substantive reconciliation; "
                        "return to Plan before production edits, preserving review gates."
                    ),
                    parameters=(
                        WORKSPACE, PLAN, WORK_KIND, REVIEW_CONTEXT, PLAN_CHANGE_REPORT,
                    ) + delivery_context_parameters + (
                        (ACCEPTED_PLAN_SHA256,)
                        if source == "implement" and coder_selection_enabled else ()
                    ),
                )
            )
    edges.append(
        EdgeSpec(
            key="plan_contract_revalidate",
            source="plan_contract",
            transition="revalidate",
            target="plan_revalidation",
            context="continue_session",
            context_source="node:plan",
            prompt=plan_revalidation_prompt(
                profile,
                from_review=False,
                coder_selection=coder_selection_enabled,
            ),
            transition_description=(
                "The accepted plan contract requires reconciliation; return to "
                "the retained planner before any writer starts."
            ),
            parameters=(
                WORKSPACE,
                PLAN,
                WORK_KIND,
                PLAN_ROUTE,
                PLAN_ROUTE_CONTEXT,
                PLAN_CHANGE_REPORT,
                REVIEW_CONTEXT,
                TASK_SHORT_ID,
            ) + (
                (ACCEPTED_PLAN_SHA256,) if coder_selection_enabled else ()
            ) + delivery_context_parameters,
        )
    )
    if coder_selection_enabled:
        def add_parameters(
            edge: EdgeSpec,
            additions: tuple[ParameterSpec, ...],
        ) -> EdgeSpec:
            existing = {parameter.key for parameter in edge.parameters}
            return replace(
                edge,
                parameters=edge.parameters
                + tuple(
                    parameter
                    for parameter in additions
                    if parameter.key not in existing
                ),
            )

        carried_digest_edges = {
            "plan_review",
            "plan_review_accept",
            "plan_review_revalidate",
            "plan_review_resume_continue",
            "plan_review_resume_verify",
            "plan_revalidation_review",
            "plan_revalidation_resume_continue",
            "plan_revalidation_resume_verify",
            "plan_review_needs_user_action",
            "plan_revalidation_needs_user_action",
            "plan_needs_user_action",
            "plan_contract_continue_revalidate",
            "plan_contract_verify_revalidate",
            "plan_contract_fix_revalidate",
            "fix_replan",
            "compliance_replan",
        }
        if fresh_writers:
            carried_digest_edges.update(
                {
                    "plan_review_resume_fix_continue",
                    "plan_revalidation_resume_fix_continue",
                }
            )
        branch_edges = {
            "plan_contract_branch_identity",
            "branch_identity_implement",
            "branch_identity_resolution",
            "branch_identity_retry",
            "branch_identity_resolution_needs_user_action",
        }
        selected_entry_edges = {
            "plan_contract_implement",
            "branch_identity_implement",
        }
        updated_edges: list[EdgeSpec] = []
        for edge in edges:
            current = edge
            if edge.key in carried_digest_edges:
                digest_fields = (ACCEPTED_PLAN_SHA256,)
                if edge.key == "plan_review_accept":
                    digest_fields += (REVIEWED_NORMALIZED_SHA256,)
                current = add_parameters(current, digest_fields)
            if edge.key in branch_edges:
                branch_parameters = (
                    (BLOCKER,) + branch_identity_parameters
                    if edge.key in {
                        "branch_identity_resolution",
                        "branch_identity_resolution_needs_user_action",
                    }
                    else branch_identity_parameters
                )
                current = replace(
                    current,
                    parameters=branch_parameters,
                )
            if edge.key in selected_entry_edges:
                current = replace(
                    current,
                    context="new_session",
                    context_source="immediate_source",
                    prompt=implement_prompt(
                        profile,
                        fresh_session=False,
                        coder_selection=True,
                        first_selected_session=True,
                    ),
                    assignee_selection="previous_node",
                    thinking_selection="previous_node",
                )
                current = add_parameters(
                    current,
                    (TARGET_ASSIGNEE, TARGET_THINKING),
                )
            elif edge.key in {
                "plan_contract_continue_implement",
                "plan_contract_checked_continue",
            }:
                current = replace(
                    current,
                    prompt=implement_prompt(
                        profile,
                        coder_selection=True,
                    ),
                )
            if edge.key == "implement_continue":
                current = add_parameters(current, (ACCEPTED_PLAN_SHA256,))
            elif edge.key == "implement_verify":
                current = add_parameters(current, (ACCEPTED_PLAN_SHA256,))
            elif edge.key == "implement_needs_user_action":
                recovery_prompt = (current.prompt or "").replace(
                    "Use the retained compacted context",
                    "Continue in this same retained Implement Session",
                )
                current = replace(current, context="continue_session")
                current = add_parameters(
                    current,
                    selection_carriers,
                )
                current = replace(
                    current,
                    prompt=recovery_prompt
                    + """

Preserve the exact `task_short_id` and `accepted_plan_sha256`. On resume,
revalidate the accepted selection and this same Session's effective model,
reasoning level, context window, and identity before editing. Do not start or
re-lock another writer."""
                )
            elif edge.key in {
                "plan_needs_user_action",
                "plan_review_needs_user_action",
                "plan_revalidation_needs_user_action",
            }:
                current = add_parameters(current, selection_carriers)
            updated_edges.append(current)
        edges = updated_edges

        edges.extend(
            [
                *(
                    [
                        EdgeSpec(
                            key="branch_identity_revalidate",
                            source="branch_identity",
                            transition="revalidate",
                            target="plan_revalidation",
                            context="continue_session",
                            context_source="node:plan",
                            prompt=plan_revalidation_prompt(
                                profile,
                                from_review=False,
                                coder_selection=True,
                            ),
                            transition_description=(
                                "The accepted plan or selection changed before "
                                "the first writer; return to reviewed planning."
                            ),
                            parameters=(
                                WORKSPACE,
                                PLAN,
                                WORK_KIND,
                                PLAN_ROUTE,
                                PLAN_ROUTE_CONTEXT,
                                PLAN_CHANGE_REPORT,
                                REVIEW_CONTEXT,
                                TASK_SHORT_ID,
                                ACCEPTED_PLAN_SHA256,
                            ) + delivery_context_parameters,
                        ),
                        EdgeSpec(
                            key="branch_identity_resolution_revalidate",
                            source="branch_identity_resolution",
                            transition="revalidate",
                            target="plan_revalidation",
                            context="continue_session",
                            context_source="node:plan",
                            prompt=plan_revalidation_prompt(
                                profile,
                                from_review=False,
                                coder_selection=True,
                            ),
                            transition_description=(
                                "Branch retry exposed accepted-plan or choice "
                                "drift; re-review before proceeding."
                            ),
                            parameters=(
                                WORKSPACE,
                                PLAN,
                                WORK_KIND,
                                PLAN_ROUTE,
                                PLAN_ROUTE_CONTEXT,
                                PLAN_CHANGE_REPORT,
                                REVIEW_CONTEXT,
                                TASK_SHORT_ID,
                                ACCEPTED_PLAN_SHA256,
                            ) + delivery_context_parameters,
                        ),
                    ]
                    if branch_identity_enabled
                    else []
                ),
                EdgeSpec(
                    key="implement_revalidate",
                    source="implement",
                    transition="revalidate",
                    target="plan_revalidation",
                    context="continue_session",
                    context_source="node:plan",
                    prompt=plan_revalidation_prompt(
                        profile,
                        from_review=False,
                        coder_selection=True,
                    ),
                    transition_description=(
                        "The retained writer cannot prove its accepted choice "
                        "and requests reviewed Plan revalidation."
                    ),
                    parameters=(
                        WORKSPACE,
                        PLAN,
                        WORK_KIND,
                        PLAN_ROUTE,
                        PLAN_ROUTE_CONTEXT,
                        PLAN_CHANGE_REPORT,
                        REVIEW_CONTEXT,
                        TASK_SHORT_ID,
                        ACCEPTED_PLAN_SHA256,
                    ) + delivery_context_parameters,
                ),
            ]
        )
    # Add owner guidance after coder selection has finalized writer prompts.
    # Invalid-workspace recovery remains metadata-only until the root is valid.
    edges = [
        replace(edge, prompt=f"{edge.prompt}\n\n{owner_replan_instruction(profile)}")
        if edge.target in set(replan_owners)
        and edge.prompt
        and edge.key != "dispatch_invalid_workspace"
        else edge
        for edge in edges
    ]
    if target_branch_enabled:
        updated_edges = []
        for edge in edges:
            if any(
                parameter.key in _PR_TARGET_CARRIER_FIELDS
                for parameter in edge.parameters
            ) and edge.key != "prepare_pr_no_pr" and not any(
                parameter.key == PR_BASE_BRANCH.key
                for parameter in edge.parameters
            ):
                edge = replace(
                    edge,
                    parameters=edge.parameters + (PR_BASE_BRANCH,),
                )
            updated_edges.append(edge)
        edges = updated_edges
    if source_ci and delivery_context_enabled:
        recovery_keys = {
            "ci_watch_source_changed", "ci_watch_state_changed", "ci_watch_diagnose",
            "ci_monitor_needs_user_action", "waiting_pr_needs_user_action",
        }
        edges = [
            replace(
                edge,
                parameters=tuple(p for p in edge.parameters if p.key != "ci_report")
                + (() if any(p.key == "delivery_context" for p in edge.parameters)
                   else (DELIVERY_CONTEXT,)),
                prompt=ci_recovery_prompt(edge.prompt) if edge.prompt else edge.prompt,
            ) if edge.key in recovery_keys else edge
            for edge in edges
        ]
    edges = qualify_transition_keys(edges)
    spec = WorkflowSpec(
        name=profile.workflow_name("delivery", version),
        description=(
            "Plan, implement, independently verify, fix, and deliver an "
            f"engineering change for {profile.project_name}."
        ),
        execution_target=profile.execution_target("delivery"),
        nodes=tuple(nodes),
        edges=tuple(edges),
    )
    spec.validate()
    return spec


def build_canary_workflow(
    profile: ProjectProfile,
    version: int,
) -> WorkflowSpec:
    capabilities = dict(profile.capabilities)
    capabilities.update(
        {
            "pull_requests": False,
            "ci_monitoring": False,
        }
    )
    policies = dict(profile.policies)
    policies["smoke"] = "disabled"
    procedures = dict(profile.procedures)
    procedures.update(
        {
            "plan": "",
            "implement": "",
            "fix": "",
            "smoke": "",
            "ship": "",
            "ci": "",
            "waiting_pr": "",
        }
    )
    canary_profile = replace(
        profile,
        release_topology="none",
        capabilities=capabilities,
        policies=policies,
        procedures=procedures,
        work_kinds={
            key: replace(kind, plan="", implement="")
            for key, kind in profile.work_kinds.items()
        },
    )
    delivery_spec = build_delivery_workflow(canary_profile, version)
    spec = replace(
        delivery_spec,
        name=profile.workflow_name("canary", version),
        description=(
            "Exercise planning, implementation continuation, deterministic "
            f"verification, fan-out, Join, and cleanup for {profile.project_name}."
        ),
        execution_target=profile.execution_target("canary"),
    )
    spec.validate()
    return spec


def build_smoke_lab_workflow(
    profile: ProjectProfile,
    label: str = "",
) -> WorkflowSpec:
    if profile.smoke_policy() != "conditional":
        raise SpecError("smoke-lab requires conditional Smoke policy")

    capabilities = dict(profile.capabilities)
    capabilities.update(
        {
            "pull_requests": False,
            "ci_monitoring": False,
        }
    )
    procedures = dict(profile.procedures)
    procedures.update(
        {
            "plan": "",
            "implement": "",
            "fix": "",
            "ship": "",
            "ci": "",
            "waiting_pr": "",
        }
    )
    lab_profile = replace(
        profile,
        release_topology="none",
        capabilities=capabilities,
        procedures=procedures,
        work_kinds={
            key: replace(kind, plan="", implement="")
            for key, kind in profile.work_kinds.items()
        },
    )
    delivery_spec = build_delivery_workflow(lab_profile, 1)
    spec = replace(
        delivery_spec,
        name=profile.workflow_name("smoke-lab", label=label),
        description=(
            "Exercise conditional runtime Smoke routing without PR or CI "
            f"delivery stages for {profile.project_name}."
        ),
        execution_target=profile.execution_target("smoke-lab"),
    )
    spec.validate()
    return spec


def agent_node(key: str, display_name: str, role: str) -> NodeSpec:
    return NodeSpec(
        key=key,
        kind="agent",
        display_name=display_name,
        agent=role,
        completion_mode="shell_command",
    )


def transition_key(source: str, outcome: str) -> str:
    return f"{source}_{outcome}"


def qualify_transition_keys(edges: list[EdgeSpec]) -> list[EdgeSpec]:
    parameters_by_source: dict[str, dict[str, list[str]]] = {}
    for edge in edges:
        fields = parameters_by_source.setdefault(edge.source, {}).setdefault(
            edge.transition, [],
        )
        for parameter in edge.parameters:
            if parameter.key not in fields:
                fields.append(parameter.key)

    qualified: list[EdgeSpec] = []
    for edge in edges:
        prompt = edge.prompt
        if prompt:
            for outcome in sorted(
                parameters_by_source.get(edge.target, ()),
                key=len,
                reverse=True,
            ):
                prompt = prompt.replace(
                    f"`{outcome}`",
                    f"`{transition_key(edge.target, outcome)}`",
                )
            # The graph, not handwritten stage recipes, owns output fields.
            contracts = []
            for outcome, keys in parameters_by_source.get(edge.target, {}).items():
                fields = ", ".join(f"`{key}`" for key in keys) or "(none)"
                contracts.append(
                    f"- `{transition_key(edge.target, outcome)}`: {fields}."
                )
            if contracts:
                prompt += (
                    "\n\nCompletion fields from the selected graph contract "
                    "(emit only the selected transition's fields; commentary is optional):\n"
                    + "\n".join(contracts)
                )
        qualified.append(
            replace(
                edge,
                transition=transition_key(edge.source, edge.transition),
                prompt=prompt,
            )
        )
    return qualified


def recovery_edge(
    node_key: str,
    *,
    profile: ProjectProfile | None = None,
    context: str = "compact_and_continue_session",
    fresh_session: bool = False,
    carry_delivery_context: bool = False,
    extra_parameters: tuple[ParameterSpec, ...] = (),
    extra_prompt: str = "",
) -> EdgeSpec:
    extra_contract = f"\n{extra_prompt}" if extra_prompt else ""
    delivery_context_contract = (
        "\nIncoming delivery_context: {{.Params.delivery_context}}\nPreserve "
        "the exact JSON string and every nested packet byte."
        if carry_delivery_context
        else ""
    )
    cancellation_contract = ""
    if node_key in {
        "plan",
        "plan_review",
        "plan_revalidation",
        "implement",
        "fix",
        "verification_gate",
        "evidence_repair",
    }:
        cancellation_contract = (
            "\nIf the latest user instruction explicitly cancels the task, choose "
            "`wont_do` and provide `closure_reason`."
        )
    if fresh_session:
        if profile is None:
            raise ValueError(
                f"fresh recovery for {node_key!r} requires a project profile"
            )
        manifest_contract = {
            "implement": ("implement", "implement", "implementation"),
            "fix": ("implement", "fix", "implementation"),
            "evidence_repair": (
                "implement",
                "evidence_repair",
                "implementation",
            ),
        }.get(node_key)
        if manifest_contract is None:
            raise ValueError(
                f"fresh recovery context is not defined for {node_key!r}"
            )
        recovery_context = context_instruction(profile, *manifest_contract)
        stage_contract = {
            "implement": """
Implement exactly one unchecked ready writer-owned plan step. Runtime Smoke and
other workflow-owned review or delivery items are downstream scope even when a
legacy plan renders them as unchecked entries. Do not execute or mark those
items complete. Update the plan before choosing `continue_implementation` for
another fresh writer session; choose `verify` when no writer-owned step
remains.""",
            "fix": """
Apply exactly one independently verifiable remaining fix slice. Update the
authoritative fix checklist. Choose `continue_fix` with `workspace_path`,
`task_short_id={{.TaskShortId}}`, and a refreshed `fix_context` containing only
the remaining findings, or choose `verify` with the same `task_short_id` plus
the complete `review_context` when no fix slice remains.""",
        }.get(node_key, "")
        prompt = (
            f"""Restart the `{node_key}` stage in a fresh bounded session after user action.

Previous blocker: {{{{.Params.blocker_reason}}}}

{recovery_context}

Read the task body, current task comments, project instructions, authoritative
task artifacts, preserved worktree diff, and existing evidence before editing
or repeating checks. If user feedback changed a product decision or acceptance
criterion, update the authoritative design/specification/plan first and
reference the exact task-comment ID. Do not restart completed work.
{stage_contract}

Approval means only that the exact reported blocker action is now complete; it
is not acknowledgement that waiting may begin. Verify resolution before
editing. If the blocker remains, preserve task scope and return
`needs_user_action` again. Do not infer approval for any broader or destructive
action.{delivery_context_contract}{extra_contract}"""
            + cancellation_contract
        )
    else:
        prompt = (
            f"""Resume the `{node_key}` stage after user action.

Previous blocker: {{{{.Params.blocker_reason}}}}

Use the retained compacted context and re-read current task comments and
project instructions. Approval means only that the exact reported blocker
action is now complete; it is not acknowledgement that waiting may begin.
Verify resolution before continuing. If the blocker remains, preserve task
scope and return `needs_user_action` again. Do not infer approval for any
broader or destructive action.{delivery_context_contract}{extra_contract}"""
            + cancellation_contract
        )
    return EdgeSpec(
        key=f"{node_key}_needs_user_action",
        source=node_key,
        transition="needs_user_action",
        target=node_key,
        context=context,
        prompt=prompt,
        requires_approval=True,
        transition_description=(
            "Work is externally blocked. Do not approve until the reported "
            "external action is complete; approval resumes this stage."
        ),
        parameters=tuple(
            {
                parameter.key: parameter
                for parameter in (
                    (BLOCKER,)
                    + extra_parameters
                    + ((DELIVERY_CONTEXT,) if carry_delivery_context else ())
                )
            }.values()
        ),
    )


def cancellation_edge(node_key: str) -> EdgeSpec:
    return EdgeSpec(
        key=f"{node_key}_wont_do",
        source=node_key,
        transition="wont_do",
        target="wont_do",
        requires_approval=True,
        transition_description=(
            "Cancel this task only after explicit user approval and record why."
        ),
        parameters=(CLOSURE_REASON,),
    )


def procedure_instruction(profile: ProjectProfile, key: str) -> str:
    path = profile.procedure(key)
    if path:
        return f"Use {path} as the project procedure."
    return "Follow the project contract and repository instructions."


def cleanup_evidence_owner_instruction(profile: ProjectProfile) -> str:
    helper = profile.command("prepare_cleanup")
    if not helper:
        return ""
    identity = (
        "`KENT_SESSION_ID` and `KENT_STEP_ID`"
        if profile.delivery_reliability_policy() == "v1" or profile.runtime_contracts_v2()
        else "`KENT_SESSION_ID`, `KENT_RUN_ID` and `KENT_STEP_ID`"
    )
    return f"""Terminal Cleanup evidence has one project-owned owner: `{helper}`.
Invoke it in the original Task root using the project Cleanup procedure.
{procedure_instruction(profile, "cleanup")}
The project command alone owns the final ordinary append and terminal seal.
Keep its payload, optional telemetry and archive protocol in that procedure.

Do not issue a standalone ledger append before the helper, after seal, or
merely to report a blocker. Until valid terminal preparation is possible,
keep preflight and blocker observations in retained Task records.
Use real current, unmodified {identity}.
Never generate, export, substitute or replace identities to bypass deduplication.
If conflicting or fabricated evidence or substituted identities are present,
preserve the evidence and block. Do not accept that seal as valid, overwrite
its history, reconstruct it or discard-and-reseal.

Only genuinely successful prior preparation may use the existing project-
validated frozen-request/report recovery, without another ordinary append.
Freeze the original root, branch and validated report before leaving the
Task worktree. Do not call relative project adapters from pinned primary
after leave or re-read its branch as the Task branch."""


def cleanup_without_preparation_instruction(
    profile: ProjectProfile,
    manifest: str,
    evidence_type: str,
) -> str:
    evidence = profile.command("evidence")
    runtime = profile.command("runtime_contracts")
    evidence_command = evidence or "configured evidence command"
    runtime_support = (
        f"`{runtime}`" if runtime else "the configured runtime-contract support module"
    )
    current = profile.delivery_reliability_policy() == "v1" or profile.runtime_contracts_v2()
    identity = (
        "`KENT_SESSION_ID` and `KENT_STEP_ID`" if current
        else "`KENT_SESSION_ID`, `KENT_RUN_ID` and `KENT_STEP_ID`"
    )
    owner_proof = (
        "verify the final event's native Session/Task binding and stored inherited Step;\n"
        "native readback does not independently authenticate the current Step"
        if current else "read back the final ordinary event's actual Kent Session/Run/Step records"
    )
    return f"""Read `{manifest}` first and stay inside its required and
conditionally triggered sources.

This profile has no non-empty `prepare_cleanup` opt-in. Do not call or invent
a preparation helper. Before recording Cleanup evidence, inspect the original
Task runtime in the original workspace with
`{evidence_command} validate --task {{{{.TaskShortId}}}} --workspace <workspace>`
and, when a ledger exists, `read` it to identify its actual final record.

If the ledger already ends in a terminal seal, recover the original successful request/report/marker
from their actually retained Task or Session sources.
Validate the ledger chain with the existing
`validate_terminal_chain`, validate the original request with
`validate_terminal_seal_request`, and validate the original report with
`validate_cleanup_report` from {runtime_support}. Require matching task
identity, an identical original report marker and ledger marker, and an exact
match between the frozen request and the marker's operation-report digests,
redaction proof and retention class. Check those digests against the retained
operation reports and {owner_proof};
non-empty identity strings alone are not proof. Use
only this existing read-only validation path. Reuse a valid prior success
without another append or seal; do not fabricate a marker or reconstruct
missing original proof from the seal.

Only for a genuinely new successful Cleanup with a validated unsealed ledger,
with no earlier Cleanup final event or partial seal attempt, record its one
ordinary event before transition using
`{evidence_command} append --task {{{{.TaskShortId}}}} --workspace <workspace>`.
Set `node_key=cleanup`, `evidence_type={evidence_type}`, and
`context.manifest_path={manifest}`; use real unmodified current
{identity}. Include the exact files
read and actual checks. Optional telemetry must remain truthful; unavailable
counters stay null. Then use the existing evidence
command's `seal` operation:
`{evidence_command} seal --task {{{{.TaskShortId}}}} --workspace <workspace>`.
Supply the truthful request derived from actual operation reports and
redaction evidence, and use the exact returned terminal marker as the final
line of the truthful cleanup report. Do not append or seal merely to report a
blocker. Treat an existing Cleanup final event without a valid seal as partial
history and block; do not append another event or retry the seal.

Missing, malformed, conflicting, unreferenced or unverifiable history,
request/report/marker bytes, native identities or provenance is a blocker:
preserve the resources and original evidence, make no append/seal attempt,
and name the concrete missing proof or external action. Never replace native
identities, discard history or reseal."""


def context_instruction(
    profile: ProjectProfile,
    manifest_key: str,
    node_key: str,
    evidence_type: str,
) -> str:
    skill_entry = (
        f"{kit_skill_entry_instruction()}\n\n"
        if manifest_key in {"review", "delivery"}
        else ""
    )
    manifest = profile.context_manifest(manifest_key)
    if node_key == "cleanup":
        owner = cleanup_evidence_owner_instruction(profile)
        if owner:
            return f"""{skill_entry}Read `{manifest}` first and stay inside its required and
conditionally triggered sources.

{owner}"""
        # Schema-3 profiles can explicitly supply runtime support without
        # adopting runtime-v2. Legacy profiles with neither capability must
        # retain ordinary Cleanup completion, not invent a seal protocol.
        if profile.runtime_contracts_v2() or profile.command("runtime_contracts"):
            return (
                skill_entry
                + cleanup_without_preparation_instruction(
                    profile,
                    manifest,
                    evidence_type,
                )
            )
    if manifest_key == "review":
        evidence_owner = (
            "the Gate node's"
            if node_key == "verification_gate"
            else "this node's"
        )
        return f"""{skill_entry}Read `{manifest}` first and stay inside its required and
conditionally triggered sources.

Only when this Workflow review node requires evidence, append only
{evidence_owner} own required evidence through the qualified configured
evidence adapter, using genuine current Task/Node/Session/Step identity. When
no evidence is required, do not append. Do not edit sources or checkpoints,
write the ledger directly, append for another producer, seal or recover it,
or make other shell writes. First-class edit tools remain disabled.

When required, pipe one non-empty JSON object to
`{profile.command("evidence")} append --task {{{{.TaskShortId}}}} --workspace
<workspace>`. Set `node_key` to `{node_key}`, `evidence_type` to
`{evidence_type}`, and `context.manifest_path` to `{manifest}`.
`context.files_read` lists other project instruction files in actual read
order; do not repeat the manifest there. Optional telemetry must be truthful;
leave unavailable counters null. Exclude secrets or broad raw evidence.
When recovering an existing record, reuse its returned sequence/hash and
continue."""
    identity_scope = (
        "Session/Step pair"
        if profile.delivery_reliability_policy() == "v1" or profile.runtime_contracts_v2()
        else "Kent run"
    )
    return f"""{skill_entry}Read `{manifest}` first and stay inside its required and
conditionally triggered sources.

Before transition, pipe one non-empty JSON object to
`{profile.command("evidence")} append --task {{{{.TaskShortId}}}} --workspace
<workspace>`. Set `node_key` to `{node_key}`, `evidence_type` to
`{evidence_type}`, and `context.manifest_path` to `{manifest}`.
`context.files_read` lists other project instruction files in actual read
order; do not repeat the manifest there. Optional telemetry must be truthful;
leave unavailable counters null. Exclude secrets or broad raw evidence.
Append is idempotent for the current
{identity_scope}; on recovery, reuse the returned sequence/hash and continue."""


def kit_skill_entry_instruction() -> str:
    return """Before relying on shared Kit sources,
first read the installed `kent-engineering-kit` skill's `SKILL.md`, then follow its source-binding
instructions to locate and read those sources. If the skill entrypoint, source
binding, or a required source is unavailable, block only the dependent action
and continue independent work."""


def branch_identity_resolution_prompt(
    profile: ProjectProfile,
    *,
    coder_selection: bool = False,
) -> str:
    selection_contract = (
        """

Preserve `task_short_id`, `accepted_plan_sha256`, `plan_route`,
`plan_route_context`, and `review_context` byte-for-byte. Branch retry is not
authority to select or replace a coder. If the accepted normalized plan,
selection, or its task binding changed, choose `revalidate` with the exact
change report; the retained Plan session will review and obtain fresh human
approval before any writer starts. Otherwise choose `retry` only after the
reported branch blocker is resolved. The script revalidates the snapshot and
rejects any retained implementation writer before dispatch.
"""
        if coder_selection
        else ""
    )
    return f"""Resolve the deterministic branch-identity blocker before Implement.

{context_instruction(profile, "delivery", "branch_identity_resolution", "delivery")}{delivery_context_input(profile)}Project branch policy: `{profile.branch_identity_policy()}`.
Reported blocker: {{{{.Params.blocker_reason}}}}

Inspect repository and task state
without editing files, renaming branches, deleting refs, pushing, or starting
implementation. Explain the exact collision or identity ambiguity and the
smallest safe external action that resolves it.

Choose `needs_user_action` with an updated `blocker_reason` while the blocker
remains. After the user or external system resolves that exact blocker, verify
it and choose `retry`. Choose `wont_do` only for an explicit cancellation and
provide `closure_reason`.{delivery_context_tail(profile)}""" + selection_contract


def checkpoint_instruction(profile: ProjectProfile, stage: str) -> str:
    stage_details = {
        "fix": (
            "Record the pinned baseline, supplied findings, completed fix "
            "slices, fresh green checks, remaining findings, mutation ledger, "
            "and one next permitted action. A same-node `continue_fix` action "
            "is consumed when the task re-enters Fix. If the current "
            "`fix_context` still contains findings, do not repeat that "
            "transition or append bookkeeping-only evidence: rewrite "
            "`next_action` to one concrete remaining slice and complete it in "
            "the current session."
        ),
        "smoke": (
            "Record the authorized acceptance stage, exact runtime target, "
            "completed actions, sanitized evidence, remaining scenarios, "
            "external effects, restoration, and next action. The project "
            "Smoke procedure and resource adapter own lease acquisition, "
            "native identity binding, recovery and release. Preserve exact "
            "resource/owner/token state privately in `stage_data` before "
            "runtime effects; reconcile it before reuse, never infer ownership "
            "from occupancy or acquire around an unresolved lease. Require "
            "release readback and retain unresolved ownership as a blocker. "
            "Never expose tokens in reports or evidence."
        ),
    }[stage]
    return f"""Maintain the ignored `{stage}` checkpoint with
`{profile.command("checkpoint")} <validate|read|write> --stage {stage} --task
{{{{.TaskShortId}}}} --workspace <workspace>`. Reconcile an existing checkpoint
before repeating work. Pipe one JSON object to `write` before the first
expensive or mutating action, after each bounded stage, and before transition.
It contains `completed`, `remaining`, and `mutation_ledger` arrays plus one
non-empty `next_action`. {stage_details} Exclude credentials, authenticated
content, raw logs, and broad device data."""


def work_kind_catalog(profile: ProjectProfile) -> str:
    return "\n".join(
        (
            f"- `{key}` — {work_kind.description.rstrip('.')}; "
            + (
                f"Plan: `{work_kind.plan}`; Implement: `{work_kind.implement}`."
                if work_kind.plan or work_kind.implement
                else "Use this stage's instructions."
            )
        )
        for key, work_kind in profile.work_kinds.items()
    )


def work_kind_plan_instruction(profile: ProjectProfile) -> str:
    return f"""Select exactly one supported `work_kind` before detailed planning:

{work_kind_catalog(profile)}

A standalone `work_kind: <key>` declaration in the task body is an explicit
human routing decision and wins when the key is supported. Otherwise classify
conservatively from the requested outcome and source context. Do not use the
Jira issue type alone as proof. If the kind is unsupported, multiple kinds are
materially coupled, or the classification remains ambiguous, complete with
`needs_user_action` and ask for one supported key.

After selection, load the mapped Plan procedure as a procedure module. Some
project procedures also document execution; this Plan node must use only their
bootstrap, discovery, design/specification, analysis, planning, and plan-review
sections. The generated stage contract overrides any instruction to edit
production files, execute plan steps, invoke nested prompt flows, or launch a
separate delivery workflow."""


def work_kind_implement_instruction(profile: ProjectProfile) -> str:
    mapping = (
        "Read the selected key's Implement mapping from `.kent/workflow-profile.toml`."
        if any(kind.implement for kind in profile.work_kinds.values())
        else "No product Implement procedure is selected; use this isolated stage contract."
    )
    instruction = f"""Selected work kind: `{{{{.Params.work_kind}}}}`.

{mapping}
The key must exist and match the authoritative plan; otherwise report the
concrete routing blocker without reclassifying it. Load only that Implement
procedure, if non-empty. When that file combines planning and
execution, skip its discovery, planning, audit-launch, and scope-confirmation
stages: execute exactly one already-approved unchecked plan step."""
    if profile.delivery_reliability_policy() == "v1":
        instruction += "\n\n" + guard_owned_work_kind_instruction()
    return instruction


def guard_owned_work_kind_instruction() -> str:
    return """Preserve the selected work kind without reclassifying it.
Do not emit `work_kind` on `continue_implementation` or `verify`: their
Plan Contract guard restores it from the accepted snapshot and carries it to
the next writer. Emit `work_kind` on `needs_user_action` and `replan`,
where the transition declares it; preserve all other declared carriers."""


def work_kind_recovery_instruction(profile: ProjectProfile) -> str:
    mapping = (
        "Use its mapped Implement procedure from `.kent/workflow-profile.toml`."
        if any(kind.implement for kind in profile.work_kinds.values())
        else "Use this isolated stage contract, not a product Implement procedure."
    )
    if profile.delivery_reliability_policy() == "v1":
        return mapping + "\n\n" + guard_owned_work_kind_instruction()
    return """Preserve the selected `{{.Params.work_kind}}` routing key. """ + mapping + """ Do not
reclassify the task during recovery. Emit only the selected graph transition's
declared fields."""


def jira_reference_instruction(profile: ProjectProfile) -> str:
    if "jira_api" not in profile.required_adapters:
        return ""

    return f"""

When the source contains a Jira issue, use `{profile.adapter("jira_api")}` and
inspect its normalized `issue_links` before relying on semantic repository
search. The implementation scope contains only root issues explicitly supplied
by the task source/body or an exact human-authored task comment. Parent,
linked, cloned, related, dependency, and sibling issues are evidence or
dependency context only; they do not silently add requirements or implementation
scope. Record root scope, related evidence, and deferred/out-of-scope issues
separately. If the root issue depends on unresolved product requirements owned
by a related issue, report that dependency instead of absorbing it.

Follow related issues only one graph level and within the project procedure's
research limit. Project procedures own reference-repository discovery and
evidence recipes; related implementations do not supersede this task's
explicit decisions, current design or API contracts."""


def sentry_reference_instruction(profile: ProjectProfile) -> str:
    if "sentry_issues" not in profile.required_adapters:
        return ""

    return f"""

When the task source/body explicitly identifies a Sentry issue URL or
unambiguous numeric Sentry issue ID, use `{profile.adapter("sentry_issues")}` to
load normalized, bounded evidence under its project procedure. Persist only
sanitized source context in normal planning artifacts; never raw request,
user, breadcrumb, variable or context payloads. External mutations, including
mark-seen, require applicable original authority and the adapter's effect
contract. This generated prompt grants no mutation permission. Do not resolve
or mute an issue in Plan."""


def lifecycle_feasibility_instruction() -> str:
    return """Before accepting an execution plan, check the whole required lifecycle
against the Task's actual authority and project profile: deterministic checks,
runtime evidence, publication/PR/CI order and cleanup/resource preservation.
Use accessible graph/procedure facts; do not execute those effects in Plan.
Resolve technical ordering within authorized scope yourself. Ask only for the
exact new scope, risk, cost or authority decision, not permission to inspect or
repeat a decision already established. Explain known consequences of one
decision together; keep independent product decisions separate. If the graph
has no executable route, report that capability gap instead of asking the user
to choose an ordering that the graph cannot execute."""


def plan_storage_guidance(profile: ProjectProfile) -> str:
    # Resolve task-fixed storage before selecting task-plan paths.
    if profile.delivery_reliability_policy() != "v1":
        return """Read the accepted `.kent/runtime/{{.TaskShortId}}/plan-contract.json`
without writing it. Verify schema_version=1, task_short_id={{.TaskShortId}},
and the normalized plan digest; keep plan_path inside the Task workspace."""
    if profile.project_memory is not None:
        return """Resolve task-fixed storage mode with the configured qualified
`workflow-project-memory` adapter and its `resolve-task` operation before
choosing a plan path, using the exact native Task/workspace binding.
For R2, use `check-plan` with the accepted coder selection (or null when
disabled) and `resolve-plan` with the supplied immutable plan_path. The
external immutable accepted snapshot is authoritative; validate it and the
current working head and preserve original approval/review locators. Verify
the accepted work_kind is declared by the project profile.
The local `plan-contract.json` is a disposable cache: do not require or recreate it.
When R2 resolution or accepted state is unavailable, never fall back to legacy
storage. Only for a Task confirmed as inventoried legacy, use its existing
`.kent/runtime/{{.TaskShortId}}/plan-contract.json` snapshot without writing."""
    return """This profile has not adopted the project-memory adapter;
do not invoke an absent helper. Use the local
`.kent/runtime/{{.TaskShortId}}/plan-contract.json` snapshot only for a Task
confirmed as inventoried legacy. If the Task is R2 or its mode cannot be
proven, block; do not assume legacy mode or create a fallback."""


def owner_replan_instruction(profile: ProjectProfile | None = None) -> str:
    continuity = (
        "\nPreserve incoming delivery_context exactly."
        if profile is not None and _delivery_context_enabled(profile) else ""
    )
    selection_contract = (
        "\nFor complexity-selected coding, provide `accepted_plan_sha256` from "
        "the verified task-bound snapshot's `normalized_sha256`; never use "
        "`not-applicable` when an accepted snapshot exists. Preserve this "
        "authoritative digest through revalidation, review and approval."
        if profile is not None and profile.coder_selection_policy() == "complexity"
        else ""
    )
    if profile is not None and profile.delivery_reliability_policy() == "v1":
        snapshot_instruction = plan_storage_guidance(profile) + """
For R2, use the returned current immutable working_path and accepted work_kind
for replanning. For confirmed legacy, verify schema_version=1,
task_short_id={{.TaskShortId}}, and the normalized_plan UTF-8 SHA-256 against
normalized_sha256. Resolve plan_path as a regular non-escaping workspace file
(or the explicitly authorized not-applicable sentinel); verify work_kind is
declared by the project profile.
"""
    else:
        snapshot_instruction = """Read the accepted
`.kent/runtime/{{.TaskShortId}}/plan-contract.json` in the native Task's
repository root, without writing it. Verify schema_version=1,
task_short_id={{.TaskShortId}}, and SHA-256 of normalized_plan UTF-8 against
normalized_sha256. Resolve plan_path within that root as a regular non-escaping
file (or the existing explicitly authorized not-applicable sentinel); verify
work_kind is declared by the project profile. Do not guess from Session memory.
"""
    skill_entry = (
        f"\n\n{kit_skill_entry_instruction()}"
        if profile is not None
        else ""
    )
    return skill_entry + "\n\n" + """Choose `replan` before production edits when a concrete finding or verified
new authority requires substantive reconciliation of the accepted execution
plan. This route returns work to its planner; it grants no expanded source,
publication or resource authority. Ordinary within-contract repairs stay in
Fix; packaging-only defects stay in Evidence Repair. Unknown human authority
still requires its exact missing decision, not a fabricated approval.
""" + snapshot_instruction + """If snapshot, identity, path or authority is missing or contradictory, first
recover accessible bindings read-only through the qualified Durable Context
recovery route and original decision/evidence locators. Summaries and caches
are not original authority. Keep production edits blocked until the required
bindings are proven. Route technical gaps through an existing capable recovery
owner; ask the user only for a genuinely missing decision or external action.
Never invent a transition. If this node can retain blocked work only through
needs_user_action, use it with the precise unresolved gap and responsible owner;
approval alone does not repair missing tooling or contradictory evidence.
For replan provide workspace_path, snapshot plan_path and work_kind,
review_context with remaining actionable findings/evidence/authority locators,
and plan_change_report explaining the material discrepancy. Label semantic
findings agent-reported, not deterministic hash drift. Repeated approval IDs do not resolve
an unchanged blocker; an already-made decision needs an executable owner route,
not another question about whether to respect it.""" + selection_contract + continuity


def owner_replan_prompt(profile: ProjectProfile) -> str:
    prompt = plan_revalidation_prompt(
        profile,
        from_review=False,
        coder_selection=profile.coder_selection_policy() == "complexity",
    )
    prompt = prompt.replace("{{.Params.plan_route_context}}", "not-applicable")
    prompt = prompt.replace("{{.Params.plan_route}}", "continue")
    prompt = prompt.replace(
        "Detected plan-contract change:",
        "Agent-reported substantive replanning finding (not a drift attestation):",
    )
    return prompt + """

This entry precedes source repair. Reconcile the reported execution-plan
problem; do not infer permission to expand production scope. The route after
normal project review and acceptance is continue, with route context
not-applicable. Before review_plan, persist remaining actionable findings,
evidence pointers and permission boundaries in the authoritative plan or
existing discoverable report-only artifact: review_context is NOT carried
into Implement by the continue route. Preserve completed work and immutable
baseline. If only downstream ordering changed and no writer-owned step
remains, state that explicitly so Implement proceeds to normal verification
without fictitious edits or repeating completed steps. Do not fabricate
completed CI/Smoke or skip any project acceptance gate."""


def plan_prompt(
    profile: ProjectProfile,
    *,
    recovery_aware: bool = False,
    coder_selection: bool = False,
) -> str:
    recovery_contract = ""
    if recovery_aware:
        recovery_contract = """

If recovery inputs identify prior work, inspect each supplied item
independently:

- If a checkpoint ref is supplied, verify HEAD exactly matches it; inspect its
  diff, tests, and evidence.
- If a source task is identified, read its body without modifying or canceling
  it.
- If exact task-comment IDs are supplied, read the exact referenced comments
  without modifying or canceling the source task.
- Update one authoritative design/spec/plan; cite human comment IDs for
  decisions and explicitly supersede conflicts.

A source-task reference or comment IDs alone do not establish preserved
implementation. Treat it as preserved only when HEAD matches a supplied
checkpoint or concrete source diff/evidence confirms it. Plan only remaining
verifiable work; never reset, revert, or reimplement preserved code during
Plan."""
        recovery_contract += """

If the task body says the recovery Plan must stop for confirmation, complete
through `needs_user_action` with a concise artifact/remaining-work summary and
an explicit confirmation request. Do not choose `review_plan` in that Plan
run."""
    context_contract = ""
    if _delivery_context_enabled(profile):
        context_schema = (
            "workflow-delivery-context-v2"
            if _delivery_target_family(profile)
            else "workflow-delivery-context-v1"
        )
        if recovery_aware:
            context_contract = f"""If no lifecycle packet was supplied, initialize
`delivery_context` before the first Plan transition, including a recovery
transition. Use the compact JSON string
`{{"schema":"{context_schema}","phase":"pre_pr"}}`. On the first
`review_plan` handoff, provide it unchanged. Never add placeholder PR or CI
fields. On every later transition, preserve the exact incoming `delivery_context`
string."""
        else:
            context_contract = f"""If no lifecycle packet was supplied, initialize
`delivery_context` as the compact JSON string
`{{"schema":"{context_schema}","phase":"pre_pr"}}`. On the first
`review_plan` handoff, provide it unchanged. Never add placeholder PR or CI
fields. On recovery or any later transition, preserve the exact incoming
`delivery_context` string."""
    delivery_context_field = (
        ", `delivery_context`" if _delivery_context_enabled(profile) else ""
    )
    context_contract_section = (
        "\n\n" + context_contract + "\n\n"
        if context_contract
        else "\n\n"
    )
    coder_selection_contract = ""
    if coder_selection:
        coder_selection_contract = """

For configured complexity selection, make the conservative decision in the
authoritative plan using exactly one fenced `coder-selection-v1` JSON object.
Bind `task_short_id` to this Task, use only `simple` or `complex`, and give a
concise evidence-based rationale. Simple is local work with an understood
solution and deterministic checks, without public-contract, workflow-graph,
migration, concurrency, or security changes; every other case or uncertainty
is complex. Never put a model or role string in the decision. Before the first
accepted snapshot, set `accepted_plan_sha256` to the literal
`not-applicable`; during revalidation preserve the incoming accepted digest
unchanged until the replacement plan is accepted."""

    return f"""Plan {{{{.TaskShortId}}}}: {{{{.TaskTitle}}}}

{context_instruction(profile, "plan", "plan", "plan")}

{work_kind_plan_instruction(profile)}
{jira_reference_instruction(profile)}
{sentry_reference_instruction(profile)}

Task body:
{{{{.TaskBody}}}}

Keep discovery, design/spec ingestion, decisions, and implementation planning in
this one Plan session. Ask questions when a product decision is required. Do not
invoke nested prompt flows and do not implement production changes.
{lifecycle_feasibility_instruction()}
Before planning implementation, write an explicit scope boundary containing the
included root source IDs, related evidence, dependencies, and deferred or
out-of-scope issues. A relationship, shared parent, common design file, or
adjacent implementation does not merge issue scopes.

Inventory dependency and cross-module impact before implementation. SDK or
schema upgrades, generated-contract changes, and adaptations in modules outside
the root issue must be identified as either a required dependency adaptation or
a separate deferred task. Do not silently widen product behavior to make a
dependency compile. Apply project architecture and source-contract standards
through the declared planning procedure.

Assign every required evidence artifact an owner and a production-edit
boundary. If the plan requires a pre-edit red run, make it the first
writer-owned step and require capture before any production edit. The plan must
not turn a later failure to capture agent-owned evidence into a user approval.
Any design, specification, or plan that narrows, replaces, or claims to
supersede the task body must cite the exact human-authored task-comment ID or
another explicit authoritative source. Agent-authored comments, implementation
inference, and unsupported claims that "the user clarified" are not product
authority. First recover accessible original sources before treating missing
agent bookkeeping as absent authority; use `needs_user_action` only for a
remaining real decision or unavailable necessary authority.

Use optional bounded read-only critique when it resolves a concrete risk or
decision. It does not replace independent review or human authority; preserve
mandatory project review gates.
Keep exact human sources in the plan or discoverable Task evidence;
`review_context` is not the sole durable authority carrier. Link human
acceptance to its exact agent proposal. Recover native evidence before asking
again; agent summaries or truncated sources cannot supply missing consent.
{recovery_contract}{context_contract_section}Complete with `review_plan` only when the plan has no unresolved product, API,
UX, or safety ambiguity. `workspace_path` is the repository or
managed-worktree root; it is never `.todo/<feature>` or another artifact
directory. Provide that root plus `plan_path`, selected `work_kind`,
`plan_route=start`, `plan_route_context=not-applicable`,
`task_short_id={{{{.TaskShortId}}}}`{delivery_context_field}, and a concise `review_context` naming the
governing authority, source IDs, acceptance criteria, planned evidence, and
risks. Use the literal `not-applicable` as `plan_path` only when the project
contract explicitly allows planless work. Kent transition parameters must be
non-empty.
Complete with `needs_user_action` and `blocker_reason` for an external blocker.
Choose `wont_do` only for an explicit cancellation decision and provide
`closure_reason`.""" + coder_selection_contract


def implement_prompt(
    profile: ProjectProfile,
    *,
    fresh_session: bool = False,
    coder_selection: bool = False,
    first_selected_session: bool = False,
) -> str:
    fresh_contract = ""
    if fresh_session:
        fresh_contract = """

This is a fresh writer session. Treat the current worktree, task comments, and
authoritative plan as discovery entry points, not exhaustive original authority.
Follow relevant original decision/evidence locators through the qualified
Durable Context recovery route before editing or repeating a question.
Summaries and caches do not replace original authority. Inspect existing changes
and evidence before editing; do not repeat checked work. If a comment changes a
product decision, update the authoritative design/specification/plan with the
exact comment ID before implementation. Keep the step independently
verifiable; the next step runs in another fresh writer session."""
    delivery_context_contract = (
        " Preserve `delivery_context` only when the selected transition "
        "declares it. When declared, keep the exact incoming bytes.\n\n"
        if _delivery_context_enabled(profile)
        else " "
    )
    selection_contract = ""
    if coder_selection:
        selection_contract = f"""

The accepted task-bound plan snapshot is the authority for the coder choice;
do not rely on Plan conversation history or infer a replacement role.
{plan_storage_guidance(profile)}
Read the authoritative plan.
Accepted plan digest: `{{.Params.accepted_plan_sha256}}`.
Require exact task identity and `accepted_plan_sha256` equality, validate the
closed selection and its digest binding, then read the plan's original review,
human-approval, and evidence pointers. A missing, unsafe, mismatched, stale, or
changed selection blocks edits and returns through Plan revalidation.

Before editing, verify the current Session's effective execution settings and
identity against the accepted selection and configured role using supported
native readbacks. If unavailable, stale, or drifted, report the exact blocker;
never substitute settings, start a replacement Session, or silently re-lock."""
    if first_selected_session:
        selection_contract += """

This is the separately allocated first Implement Session, not a continuation
of Plan. The graph supplies the protected `target_assignee` and
`target_thinking` fields from the validated snapshot:
`{{.Params.target_assignee}}` / `{{.Params.target_thinking}}`. Verify they
match its closed mapping and that this Session identity differs from the
retained Astra Plan Session before editing."""

    selection_transition_contract = (
        """

On every `continue_implementation`, `verify`, `needs_user_action`, or
`revalidate` transition, preserve `task_short_id` and the exact incoming
`accepted_plan_sha256`. If the plan, snapshot, or choice is missing, stale, or
inconsistent, make no edit and choose `revalidate` with `plan_route=continue`,
the digest, and an exact `plan_change_report`; do not use continuation or
recovery to create another writer. For blocker recovery, emit
`needs_user_action` only for an external action and resume this same Session
after that action is verified.
"""
        if coder_selection
        else ""
    )
    return f"""Implement {{{{.TaskShortId}}}}: {{{{.TaskTitle}}}}

{context_instruction(profile, "implement", "implement", "implementation")}{delivery_context_input(profile)}Plan: {{{{.Params.plan_path}}}}. Workspace: {{{{.Params.workspace_path}}}}.

{work_kind_implement_instruction(profile)}

Use the project procedure for step selection, recipes, editing, focused checks,
and plan progress. This generated workflow's completion contract overrides any
legacy procedure transition names such as `audit`.
{fresh_contract}

Apply the `implementation-worker` role contract to exactly one ready
writer-owned plan step. Capture any planned pre-edit evidence before the first
production edit.
The step budget is not full Task authority: repair proven earlier-step Task
regressions within the full approved file scope through the existing writer/
Fix path, refreshing focused evidence. Truly closed file boundaries still
require a scope decision; baseline, foreign and unproven target-only defects
are not absorbed.

After marking that step complete, choose `continue_implementation` with
`workspace_path`, `task_short_id={{{{.TaskShortId}}}}`, and a concise
`review_context` with the completed step, changed files, checks, and next ready
step when unchecked writer-owned ready steps remain.{delivery_context_contract}Choose `verify` when every
writer-owned plan step is complete; provide the same workspace/task identity
plus `review_context` summarizing plan/spec paths, the fixed comparison point,
changed files, checks, risks, and any downstream runtime acceptance scope.
Both outcomes pass through graph-owned deterministic Plan Contract checks;
writer output cannot select accept mode or a different continuation route.
Use `needs_user_action` only for an external blocker and provide
`blocker_reason` plus the unchanged `work_kind`. Its approval is a resume signal
after the named external action is complete, not acknowledgement of waiting;
state that condition explicitly. Choose `wont_do` only for explicit
cancellation and provide `closure_reason`.""" + selection_contract + selection_transition_contract


def plan_review_prompt(
    profile: ProjectProfile,
    *,
    coder_selection: bool = False,
) -> str:
    fresh_writers = profile.writer_session_policy() == "fresh_per_slice"
    fix_resume = (
        """
Choose `plan_review_resume_fix_continue` only when this is a fresh-per-slice
profile, the exact previously accepted `fix_continue` contract and its approval
remain applicable, there are no new findings or authority/milestone changes,
and the remaining bounded Fix bundle is unchanged. Set `fix_context` to the
exact remaining bundle carried in `plan_route_context`.
"""
        if fresh_writers
        else ""
    )
    selection_context = (
        "Previous accepted plan digest: {{.Params.accepted_plan_sha256}}\n"
        if coder_selection
        else ""
    )
    selection_contract = (
        """

Independently compute the normalized plan SHA-256 using the Plan Contract
checkbox normalization. Only on `accepted`, compute and return
`reviewed_normalized_sha256`. Preserve the incoming `accepted_plan_sha256`
byte-for-byte only when the selected edge declares it. Do not emit
`reviewed_normalized_sha256` or `accepted_plan_sha256` on any outcome whose
selected edge does not declare that field. Verify the single closed,
task-bound `coder-selection-v1` object and its rationale; do not choose or
rewrite the coder role/model.
"""
        if coder_selection
        else ""
    )
    return f"""Independently review the proposed plan for
{{{{.TaskShortId}}}} without editing files.

{context_instruction(profile, "review", "plan_review", "review")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}
Plan: {{{{.Params.plan_path}}}}
Work kind: {{{{.Params.work_kind}}}}
Requested post-review route: {{{{.Params.plan_route}}}}
Route context: {{{{.Params.plan_route_context}}}}
{selection_context}Planning context: {{{{.Params.review_context}}}}

Read the task body, current human-authored comments, exact source records named
by the plan, and the plan itself. Use the read-only `spec-reviewer` contract,
adapted to the proposed plan rather than an implementation diff. Do not edit
the plan, code, task, or external systems.

Verify original human sources or retained native evidence through the
authoritative plan/evidence pointers. Recover accessible missing references
before escalating a bookkeeping gap. Preserve applicable source references,
scope, and artifact location when adding the review result to `review_context`;
do not replace them with a PASS summary. Human acceptance of an agent proposal
requires the linked proposal and acceptance. Agent paraphrases and incomplete
historical source bodies do not establish missing authority. Grill co-design
is not an independent review receipt; do not launch grill from this leaf.

Check:

- every narrowed or superseded decision has exact human authority;
- root scope is separate from related evidence and deferred work;
- product, API, UX, architecture, safety, and destructive-action choices are
  explicit rather than invented by the planner;
- acceptance criteria map to owned deterministic, review, or runtime evidence;
- dependencies and generated-contract adaptations stay bounded;
- writer steps are executable, dependency ordered, and do not hide workflow
  stages as implementation work.

Choose `plan_review_resume_continue` or `plan_review_resume_verify` only when
the exact previously accepted contract is unchanged, its prior independent
PASS review and required human approval still apply, and there are no new
findings or authority/milestone changes. Put this fresh review result in
`review_context`. Use Continue only when writer-owned work remains; use Verify
only when it is complete. These fixed-route edges always pass through the
existing Plan Contract guard; they cannot accept or continue a changed
contract. Otherwise use the existing acceptance or revalidation path.
{fix_resume}
Choose `accepted` only when implementation or verification may safely follow.
Provide `workspace_path`, `plan_path`, `work_kind`, unchanged `plan_route`,
unchanged `plan_route_context`, `task_short_id={{{{.TaskShortId}}}}`, a concise
`plan_review_report`, and refreshed `review_context` that includes the review
result. `plan_route_context` is `not-applicable` except when preserving the
remaining bounded Fix bundle for `fix_continue`.

Choose `needs_changes` for plan-contract defects and provide the same identity,
unchanged route and route context, `plan_review_report`, and `review_context`;
the retained Plan session will revise the artifact. Choose `needs_user_action`
only for a real missing product decision or external authority and provide the
preserved identity/context plus `blocker_reason`. Choose `wont_do` only for
explicit cancellation and provide `closure_reason`.""" + selection_contract


def plan_revalidation_prompt(
    profile: ProjectProfile,
    *,
    from_review: bool,
    coder_selection: bool = False,
) -> str:
    snapshot_instruction = (
        plan_storage_guidance(profile)
        + "\nCompare current plan content with the authoritative accepted snapshot. "
        "Checkbox state alone is intentionally absent from the normalized contract."
        if profile.delivery_reliability_policy() == "v1"
        else "compare the current\nplan with the prior normalized snapshot at\n"
        "`.kent/runtime/{{.TaskShortId}}/plan-contract.json`; checkbox state alone\n"
        "is intentionally absent from that contract."
    )
    finding_label = (
        "Independent Plan Review findings: {{.Params.plan_review_report}}"
        if from_review
        else "Detected plan-contract change: {{.Params.plan_change_report}}"
    )
    selection_context = (
        "Previous accepted plan digest: "
        "{{.Params.accepted_plan_sha256}}\n"
        if coder_selection
        else ""
    )
    resume_contract = """
Choose `plan_revalidation_resume_continue` or
`plan_revalidation_resume_verify` only when the current contract is still the
exact previously accepted contract, its prior independent PASS review and
required approval remain applicable, with no new findings or
authority/milestone changes. Preserve the result and original
authority references in `review_context`; Continue is only for remaining
writer-owned work, and Verify is only when it is complete. These fixed-route
edges always pass through the existing Plan Contract guard and cannot accept
or continue a changed contract. If reconciliation changed the contract, a
negative review finding remains unresolved, or prior approval/review no longer
applies, use `review_plan` so the existing independent review and acceptance
gates run.
"""
    if profile.writer_session_policy() == "fresh_per_slice":
        resume_contract += """

Choose `plan_revalidation_resume_fix_continue` only when the unchanged
previously accepted `fix_continue` contract, its independent PASS review and
approval still apply, with no new findings or authority/milestone changes.
Set `fix_context` to the exact remaining bounded Fix bundle preserved in
`plan_route_context`.
"""
    return f"""Revalidate the authoritative plan for {{{{.TaskShortId}}}}.

{context_instruction(profile, "plan", "plan_revalidation", "plan")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}
Plan: {{{{.Params.plan_path}}}}
Work kind: {{{{.Params.work_kind}}}}
Intended route after acceptance: {{{{.Params.plan_route}}}}
Route context: {{{{.Params.plan_route_context}}}}
{selection_context}Current context: {{{{.Params.review_context}}}}
{finding_label}

Continue the retained planning context. Re-read current task comments and exact
authority sources. Distinguish operational feedback that fits the accepted
contract from material changes to requirements, architecture, acceptance,
safety, or evidence. Reconcile only material changes in the authoritative
design/specification/plan and cite exact human-authored task-comment IDs or
other explicit sources. Do not edit production code or execute verification.
Preserve still-applicable human source references and artifact pointers through
revalidation; explicitly identify genuine supersession. Recover accessible
original messages, native answers/comments or retained native read results
before asking for a decision already made. Keep the exact proposal plus human
acceptance when that pair grants authority. An agent summary or truncated
historical source cannot fill missing consent. Store durable references in
the authoritative plan or discoverable project-permitted report-only evidence,
not only `review_context`; do not repurpose `plan_route_context`.
{lifecycle_feasibility_instruction()}

Optional bounded read-only critique may resolve a concrete decision or risk
when execution disproves the approach. Routine progress, hash drift, waiting
and repeated identical failures do not require another critique. Preserve
mandatory project review gates; critique never substitutes for their receipts.
When revalidation was triggered by deterministic drift, {snapshot_instruction}

{resume_contract}

Choose `review_plan` after reconciliation and provide `workspace_path`,
`plan_path`, unchanged `work_kind`, unchanged `plan_route`,
unchanged `plan_route_context`, `task_short_id={{{{.TaskShortId}}}}`, and
refreshed `review_context`. Preserve the remaining bounded Fix bundle only in
`plan_route_context` when the route is `fix_continue`; otherwise use
`not-applicable`. The independent Plan Review will run again before the new
normalized snapshot is accepted. Use `needs_user_action` with preserved
context and `blocker_reason` only for a real unresolved decision or external
authority. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`.{delivery_context_tail(profile)}""" + (
        """

Preserve `accepted_plan_sha256` byte-for-byte through review and approval; use
`not-applicable` only before any snapshot exists. A same-choice plan
revalidation must resume the original Implement Session. A changed choice or
missing retained writer is not authority to create or lock another writer;
stop for an explicit recovery decision. The independent reviewer emits a
fresh `reviewed_normalized_sha256`, and acceptance compares it with current
normalized plan bytes.
"""
        if coder_selection
        else ""
    )


def fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    bounded_contract = ""
    completion_contract = """
Treat the incoming findings as one dependency-ordered repair bundle. Deduplicate
overlap, group findings by root cause, and resolve every compatible group in
this retained Fix session. Verify each group narrowly and update the checkpoint
after meaningful work; do not transition merely to hand off bookkeeping. When
the bundle is empty, complete with `verify` and provide `workspace_path`,
`task_short_id={{.TaskShortId}}`, plus refreshed `review_context` containing
the findings, fixes, changed files, artifact paths, and focused checks."""
    if bounded:
        bounded_contract = """

This is a fresh bounded writer session. Read exact task-comment IDs referenced
by the findings and inspect the preserved diff, authoritative artifacts, and
existing evidence before editing. Material changes to product decisions,
acceptance or authority require the available replan route before source
repair; they are not permission to update the plan and immediately implement.
Do not redo completed work.

Apply exactly one independently verifiable fix slice and update the
authoritative fix checklist. A non-empty incoming `fix_context` is a work
assignment. If the inherited checkpoint only says to take `fix_continue_fix`,
that action was consumed by entry into this session: replace it with one
concrete supplied slice before work. Do not create a transition-only session or
append evidence for a bookkeeping-only handoff."""
        completion_contract = """
After one slice, choose `continue_fix` with `workspace_path`,
`task_short_id={{.TaskShortId}}`, and a refreshed `fix_context` containing
only the remaining findings. Choose `verify` only when no fix slice remains,
and provide `workspace_path`, the same `task_short_id`, plus a refreshed
`review_context` containing the findings, fixes, changed files, artifact paths,
and focused checks."""
    checkpoint = checkpoint_instruction(profile, "fix")

    return f"""Apply task-scoped fixes for {{{{.TaskShortId}}}}.

{context_instruction(profile, "implement", "fix", "implementation")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}. Findings:
{{{{.Params.fix_context}}}}

{procedure_instruction(profile, "fix")}
{bounded_contract}

{checkpoint}{delivery_context_tail(profile)}This is Source-Fix: apply the
`fix-worker` role contract only to concrete task-introduced or
task-worsened findings in `fix_context`. Baseline-wide debt, an unproven
differential, or contradictory policy is not writer scope.
An earlier step's budget does not exclude a proven regression inside the full
approved Task/file scope. Perform bounded repair and refresh focused evidence;
do not turn progress rearrangement into material plan revalidation. A truly
closed approved file boundary still requires a decision before expansion.
{completion_contract}
Use `needs_user_action` only for an external blocker and provide
`blocker_reason`. Its approval is a resume signal after the named external
action is complete, not acknowledgement of waiting; state that condition
explicitly. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`."""


def workspace_path_fix_prompt(profile: ProjectProfile) -> str:
    if profile.delivery_reliability_policy() == "v1":
        workspace_contract = """The reliability-v1 dispatch producer supplied
the canonical, native-qualified `workspace_path`:
{{.Params.workspace_path}}
Use that exact value unchanged. Treat `reported_workspace_path` only as the
rejected input for diagnosis; do not resolve from it, replace the producer-owned
root, or derive identity from the current directory.
"""
        completion_workspace = "the unchanged producer-owned root"
    else:
        workspace_contract = """Treat Kent's current task execution root as
authoritative. Resolve its canonical repository root with
`git rev-parse --show-toplevel`, or canonical current directory for an
intentional non-Git workspace. Do not move the worktree or change task artifacts.
"""
        completion_workspace = "the canonical root"
    return f"""Correct invalid workflow metadata without editing production files.

Verification dispatch rejected the reported workspace:
{{{{.Params.reported_workspace_path}}}}
Reason: {{{{.Params.fix_context}}}}{delivery_context_input(profile)}
{workspace_contract}
Do not edit production code.
This is metadata correction only; do not create or update a Fix checkpoint.
The original rejected path and reason are already preserved in the
`reported_workspace_path` and `fix_context` inputs; retain those exact source
locators. Update `review_context` only through the qualified project-owned
route, preserving its required envelope and every opaque field byte-for-byte.
Add only bounded references to those original inputs and the metadata
correction when the project contract permits it. Do not rewrite, reserialize,
or reshape an opaque packet, or emit undeclared fields.

Complete with `verify` and provide {completion_workspace} as `workspace_path`,
`task_short_id={{{{.TaskShortId}}}}`, plus refreshed `review_context`. Use
`needs_user_action` only if Kent's execution root itself is unavailable or
ambiguous. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`.{delivery_context_tail(profile)}"""


def standards_review_prompt(profile: ProjectProfile) -> str:
    return f"""Run the independent read-only Standards Review.

{context_instruction(profile, "review", "standards_review", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Review context: {{{{.Params.review_context}}}}

Apply the `standards-reviewer` role contract to the supplied task delta and its
pinned baseline. Do not edit files. Findings are data for Join, not a routing
decision.

Complete only with `reported`. Provide `standards_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `standards_report` with rule, path, and
differential evidence."""


def spec_review_prompt(profile: ProjectProfile) -> str:
    return f"""Run the independent read-only Specification Review.

{context_instruction(profile, "review", "spec_review", "review")}

Workspace: {{{{.Params.workspace_path}}}}
Review context: {{{{.Params.review_context}}}}

Apply the `spec-reviewer` role contract to the supplied task authority,
acceptance evidence, and task delta for the current authorized stage. Retain
later unverified obligations with an owner and due milestone; do not require
them early or declare them satisfied. A review-ready PR is not full business
acceptance, merge/release authority, or Task Done. This distinction applies
even without Compliance or PR nodes. Do not edit files. Findings are data for
Join, not a routing decision.

Complete only with `reported`. Provide `spec_status` as exactly `passed`,
`needs_changes`, or `blocked`, plus `review_report` with evidence and concrete
gaps."""


def verification_gate_prompt(profile: ProjectProfile) -> str:
    if profile.capability("standards_review"):
        standards = (
            "Standards status: {{.Params.standards_status}}\n"
            "Standards report: {{.Params.standards_report}}"
        )
    else:
        standards = "Standards review: not enabled by the project profile."
    spec = (
        "Spec status: {{.Params.spec_status}}\n"
        "Spec report: {{.Params.review_report}}"
        if profile.capability("spec_review")
        else "Specification review: not enabled by the project profile."
    )
    smoke_decision = smoke_decision_instruction(profile)
    delivery_context = (
        "Delivery context: "
        "{{.Params.verification_dispatch_fanout_verify.delivery_context}}\n"
        if _delivery_context_enabled(profile)
        else ""
    )
    delivery_context_handoff = (
        "Return `delivery_context` only when the selected transition declares it. "
        "When declared, preserve the exact incoming value; do not use context "
        "copied from a previous dispatch cycle."
    )
    outcome_guidance = """Return only fields declared by the selected transition.
For `needs_changes`, provide `workspace_path` plus `fix_context`.
For `needs_user_action`, provide `workspace_path`, `review_context`, and
`blocker_reason`.
For `wont_do`, provide only `closure_reason`.
For `verification_gate_retry_verification`, provide `workspace_path` and
`review_context`; preserve `delivery_context` only if that edge declares it."""
    if profile.smoke_policy() in {"conditional", "disabled"}:
        outcome_guidance += """
For `delivery_ready`, provide `workspace_path`, `review_context`, and
`smoke_rationale`."""
    if profile.smoke_policy() in {"conditional", "required"}:
        outcome_guidance += """
For `smoke_required`, provide `workspace_path`, `review_context`,
`smoke_rationale`, and `smoke_scope`."""
    return f"""Evaluate the joined verification reports without editing files.

{context_instruction(profile, "review", "verification_gate", "review")}

Workspace: {{{{.Params.verification_dispatch_fanout_verify.workspace_path}}}}
Review context: {{{{.Params.verification_dispatch_fanout_verify.review_context}}}}
{delivery_context}Verification status: {{{{.Params.verification_status}}}}
Verification report: {{{{.Params.verification_report}}}}
{standards}
{spec}

Apply the `workflow-gate` role contract. Choose `delivery_ready` or
`smoke_required` only when every enabled status is `passed` for the
current authorized stage. A verification retry is not delivery readiness. Retain
downstream unverified obligations with an owner and due milestone, including
on paths without Compliance or PR. Do not relabel a previous `blocked` report:
changed authority requires a fresh review by its owner. A review-ready PR
does not establish full business acceptance, merge/release authority, or Task
Done. Do not poll or classify PR CI here; delivery owns CI after preparation.

For `needs_changes`, emit the deduplicated, dependency-ordered repair bundle
required by the `workflow-gate` role contract.

Preserve the project-owned `review_context` envelope and its qualified update
route. Where allowed, synthesize bounded results and durable report references;
do not impose Markdown, copy full reports, or rewrite opaque producer packets.
{outcome_guidance}
{delivery_context_handoff}

{smoke_decision}

Choose `verification_gate_retry_verification` only after bounded evidence
identifies the original technical blocker, an existing capable authorized
owner's concrete resolution, changed prerequisites, and settlement of prior
owned processes/effects. Preserve the current Task/workspace, accepted scope,
real attempt/spend limits, and exact incoming `delivery_context`; add bounded
resolution references through the qualified project-owned `review_context`
route. The Script runs every enabled verification and review branch fresh.
Never relabel an old `blocked` report or reuse an old PASS as current evidence.
If owner resolution, changed prerequisites, evidence, or settlement is absent,
do not retry, poll, launch a child, repair shared infrastructure, install tools,
or invent product Fix/user authority. Technical failures belong to an existing
capable recovery owner. `needs_user_action` remains for a genuine external
action or decision; after approval, every verification branch reruns. Choose
`wont_do` only for an explicit cancellation decision."""


def smoke_prompt(profile: ProjectProfile) -> str:
    checkpoint = checkpoint_instruction(profile, "smoke")
    return f"""Run focused smoke testing for {{{{.TaskShortId}}}}.

{context_instruction(profile, "smoke", "smoke", "smoke")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}
Smoke rationale: {{{{.Params.smoke_rationale}}}}
Required scope: {{{{.Params.smoke_scope}}}}

{procedure_instruction(profile, "smoke")}

{checkpoint}Follow the project-specific browser, device, simulator, hardware, resource-lock,
build, deploy, install, launch, account, and isolation rules that apply. Do not
edit production files; route implementation findings to the single writer.
Complete with `passed` and provide `workspace_path` plus an updated
`review_context` containing the decision, rationale, tested scope, evidence,
artifacts, and untested areas. Use `needs_changes` with `workspace_path` and
`fix_context` for task code issues. Use `needs_user_action` with
`blocker_reason` for external blockers.

{smoke_cancellation_instruction()}
{delivery_context_tail(profile)}"""


def smoke_cancellation_instruction() -> str:
    return """For explicit cancellation, choose `wont_do` only after preserving
the Smoke checkpoint/evidence and settling every owned runner, process,
fixture, shell, and effect; restore changed state and verify resource release
through the existing project adapter. Safe retention is allowed only when the
existing project contract and authority qualify it; preserve concrete resource,
owner, status, safe-state proof, and next action. Never infer ownership, TTL
release, or a lease waiver. Unknown cleanup blocks cancellation. Do not route
through Janitor or claim Smoke PASS, delivery readiness, or cleanup of unrelated
resources. Record the cancellation source and settlement or qualified-retention
references in `closure_reason` without secrets. `wont_do` retains its native
approval: an original cancellation decision is not that approval, and a pending
approval must not be answered or replaced. Emit only the declared
`closure_reason` field."""


def smoke_recovery_settlement_instruction() -> str:
    return """On Smoke recovery, first reconcile the existing checkpoint, owned
resources, and prior effects. Approval means only the reported external action
is complete; it does not authorize replay of an interrupted or stopped test.
Use only a supported, authorized safety-only continuation to stop, restore,
settle, and verify owned work. If the original Smoke scope remains authorized
after settlement, continue only under its existing procedure and attempt/spend
limits. Cancellation permits safety settlement only, not ordinary runtime
replay or resumption. Preserve exact resource/owner/status evidence and the
next action through the project-owned checkpoint/evidence route. Unknown
ownership or cleanup remains blocked; do not infer a TTL release or borrow a
lease."""


def compliance_prompt(profile: ProjectProfile) -> str:
    completed_reviews = ["deterministic verification"]
    if profile.capability("standards_review"):
        completed_reviews.append("Standards Review")
    if profile.capability("spec_review"):
        completed_reviews.append("Spec Review")
    evidence_chain = ", ".join(completed_reviews)
    return f"""Run the final read-only delivery compliance review for {{{{.TaskShortId}}}}.

{context_instruction(profile, "review", "compliance", "review")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}. Final review context:
{{{{.Params.review_context}}}}

{procedure_instruction(profile, "compliance")}

Apply the `compliance_reviewer` role contract as a thin final attestation, not
another broad review. Verify the final diff, authority hierarchy,
{evidence_chain}, Gate, and any required Smoke or documented bypass. Do not
edit files or perform state-changing actions.
Assess the current authorized stage, preserving later unverified obligations
with an owner and due milestone in the project-owned review carrier. A
review-ready PR does not establish full business acceptance, merge/release
authority, or Task Done. Changed authority requires fresh owner review, not
relabeling a previous blocked report.

Choose `ship_pr` only when the final work product is compliant. Provide
`workspace_path`, a complete `review_context`, and `compliance_report`. Choose
`repair_evidence` only when the source diff and substantive deterministic,
Standards, Specification, Gate, and Smoke decisions are already valid and the
only defect is a missing, empty, stale, or internally contradictory
report/checklist/evidence package. Provide `workspace_path`, `review_context`,
and `evidence_context` naming the exact permitted artifacts and required
correction. Choose `needs_changes` for substantive task-scoped violations and
provide `workspace_path` plus `fix_context`; source fixes must rerun the full
verification flow. Choose
`needs_user_action` for missing or contradictory authority, evidence, or
external decisions and provide `workspace_path`, `review_context`, and
`blocker_reason`. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`.{delivery_context_tail(profile)}"""


def compliance_recovery_prompt(profile: ProjectProfile) -> str:
    return f"""Resume the final read-only delivery compliance review.

{context_instruction(profile, "review", "compliance", "review")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}
Final review context: {{{{.Params.review_context}}}}
Previous blocker: {{{{.Params.blocker_reason}}}}

Re-read current task comments and applicable authority sources, then verify
that the exact blocker is resolved. Do not infer approval for broader scope or
destructive actions.

{procedure_instruction(profile, "compliance")}

Choose `ship_pr` only when the final work product is compliant. Provide
`workspace_path`, a complete `review_context`, and `compliance_report`. Choose
`repair_evidence` only for packaging-only evidence defects and provide
`workspace_path`, `review_context`, and `evidence_context`. Choose
`needs_changes` with `workspace_path` and `fix_context` for substantive
task-scoped violations. Choose `needs_user_action` with `workspace_path`,
`review_context`, and `blocker_reason` if the blocker remains. Choose `wont_do`
only for explicit cancellation and provide `closure_reason`.{delivery_context_tail(profile)}"""


def evidence_repair_prompt(profile: ProjectProfile) -> str:
    return f"""Repair packaging-only workflow evidence for {{{{.TaskShortId}}}}.

{context_instruction(profile, "implement", "evidence_repair", "implementation")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}. Final review context:
{{{{.Params.review_context}}}}
Allowed evidence repair:
{{{{.Params.evidence_context}}}}

{procedure_instruction(profile, "compliance")}

This is not a source-code Fix or another verification pass. Confirm first that
the supplied context proves the source diff and substantive deterministic,
Standards, Specification, Gate, and required Smoke decisions already passed.
Edit only the exact ignored reports, summaries, checklists, or evidence indexes
named in `evidence_context`. Do not edit production source, tests, build files,
specifications, or plans; do not build, install, launch, reacquire a device, or
repeat runtime navigation.
This node's narrow write contract overrides a compliance procedure's general
read-only clause only for the exact artifacts named in `evidence_context`. Do
not create or update the Fix checkpoint from this node.

Repair missing or empty required text, reconcile contradictory status wording
with Kent's authoritative task state, and rerun only the project evidence audit
or deterministic artifact validation needed for those files.

Choose `recheck_compliance` with `workspace_path` and a refreshed
`review_context` when packaging is valid. Choose `needs_source_fix` with
`workspace_path` and `fix_context` if the supplied defect is actually
substantive and cannot be repaired inside the named evidence artifacts. Use
`needs_user_action` only for an external blocker and provide `blocker_reason`.
Also preserve `workspace_path`, `review_context`, and `evidence_context` for
recovery. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`.{delivery_context_tail(profile)}"""


def compliance_recheck_prompt(profile: ProjectProfile) -> str:
    return f"""Recheck final Compliance after packaging-only evidence repair.

{context_instruction(profile, "review", "compliance", "review")}{delivery_context_input(profile)}Workspace: {{{{.Params.workspace_path}}}}
Updated review context: {{{{.Params.review_context}}}}

{procedure_instruction(profile, "compliance")}

Apply the retained `compliance_reviewer` role context only to the repaired
evidence artifacts. Confirm they agree with already-passed substantive results;
do not repeat code, architecture, specification, build, or runtime review.

Choose `ship_pr` with `workspace_path`, `review_context`, and
`compliance_report` when the package is complete. Choose `repair_evidence`
again only for another packaging-only defect and provide `evidence_context`.
Choose `needs_changes` with `workspace_path` and `fix_context` only when the
repair exposed a substantive task defect. Use `needs_user_action` with
`workspace_path`, `review_context`, and `blocker_reason` for an external or
authority blocker. Choose `wont_do` only for explicit cancellation and provide
`closure_reason`.{delivery_context_tail(profile)}"""


def prepare_pr_prompt(profile: ProjectProfile) -> str:
    source_ci = (
        profile.source_ci_contract()
        and profile.capability("ci_monitoring")
    )
    compliance_context = (
        "Final Compliance Review: {{.Params.compliance_report}}"
        if (
            profile.capability("pull_requests")
            and profile.capability("compliance_review")
        )
        else "Final Compliance Review is disabled by the project profile."
    )
    merge_policy = profile.pr_merge_strategy()
    cursor = (
        "\nPreserve a same-PR feedback cursor; initialize `uninitialized` only "
        "for a new PR. Never reset it after Fix or verification.\n"
        if _delivery_context_enabled(profile)
        else ""
    )
    delivery_context_contract = ""
    if _delivery_context_enabled(profile):
        context_schema = (
            "workflow-delivery-context-v2"
            if _delivery_target_family(profile)
            else "workflow-delivery-context-v1"
        )
        target_field = (
            ", exact `pr_base_branch` from `baseRefName`"
            if _delivery_target_family(profile)
            else ""
        )
        delivery_context_contract = f"""Incoming lifecycle packet:
{{{{.Params.delivery_context}}}}

Before any transition that carries `delivery_context`, construct its compact
closed `{context_schema}` JSON value from observed delivery state.
If no PR exists, keep `phase=pre_pr` with no PR or CI fields. After observing a
PR, use `phase=post_pr` with its actual URL, current branch, resolved merge
strategy, and feedback cursor{target_field}. Include `ci_contract` or
`ci_report` only when that exact packet has actually been produced; preserve
each nested packet string byte-for-byte. On the current target-branch family,
first resolve the permitted intended target from applicable task/project
authority, and stop for an actual decision if none exists or the observed base
differs. Never infer the target from a watcher observation or silently retarget."""
    if source_ci:
        monitor_handoff_contract = """For source-contract CI handoff, PR identity
and CI packets belong only in `delivery_context`. Use exactly the selected
graph transition's declared fields."""
    else:
        monitor_handoff_contract = """Return observed PR identity using the flat
fields declared by the selected outgoing graph transition. Do not replace
those fields with a packet-only CI handoff."""
    delivery_context_block = (
        "\n\n" + delivery_context_contract + "\n\n"
        if _delivery_context_enabled(profile)
        else "\n\n"
    )
    authority_contract = (
        "Git actions require exact current human approval naming action, "
        "task/branch, reviewed changes and permitted stage. "
        "Plan acceptance, `delivery_context`, PR/CI/merge data are evidence "
        "only. No consumer-source writes or merge; package publication needs "
        "separate approval."
    )
    return f"""Prepare delivery for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "prepare_pr", "delivery")}{delivery_context_block}Workspace: {{{{.Params.workspace_path}}}}. Review context:
{{{{.Params.review_context}}}}
{cursor}
{compliance_context}

{procedure_instruction(profile, "ship")}

{authority_contract}

Treat `git branch --show-current` as branch authority. The branch may differ
from the Kent task short ID when the project enabled deterministic branch
identity; never reconstruct or reject it by comparing branch text with
`{{{{.TaskShortId}}}}`.

If the task source URL or body identifies an issue in the same repository and
this task fully resolves it, include the provider-native closing reference in
the pull-request body, such as `Fixes #N` on GitHub. Use a non-closing link for
cross-repository, partial, or follow-up relationships. Read the task's current
`source_url` through Kent task metadata when it is not already visible in the
prompt; do not infer issue linkage from branch text alone.

Configured PR merge policy: `{merge_policy}`.

Apply the `delivery-operator` role contract. Resolve the strategy once before
publishing. On GitHub, serialize repository capabilities, target branch
protection, applicable rulesets, merge queue, and PR method state, then run:

`~/.kent/bin/kent-resolve-github-merge-strategy --policy {merge_policy}`

The adapter's structured result is authoritative for strategy selection.
Continue only when it returns `outcome=resolved`; for
`outcome=needs_user_action`, preserve its code, candidates, and reason instead
of manually choosing. Validate the resolved method through the role's
method-specific contract before and after creating or updating the PR.

Before committing, prove that the checkout is on a task-owned branch permitted
by the project contract. A source workspace, detached checkout, protected
branch, or ambiguous branch owner must route to `needs_user_action`; never push
through that ambiguity.

{monitor_handoff_contract}

{("For the target-branch continuity family, establish the permitted target "
  "before PR creation from applicable task/project authority. Read the PR's "
  "actual `baseRefName` after creation and again before the first handoff; "
  "carry that exact value as `pr_base_branch`. Compare it on every later fresh "
  "PR read before success, merged shortcuts, or cleanup. Do not infer a target "
  "from a default branch or watcher observation. A difference requires "
  "applicable existing owner authority and fresh PR observation; when "
  "authorized, use `post_pr_target_revalidated` to change only the target in "
  "the v2 packet, preserving CI bytes and retry state. This packer mode adds "
  "no graph edge or authority."
  if _delivery_target_family(profile) else "")}

If no PR is genuinely
applicable, choose `no_pr` and provide `pr_report`; this path requires
approval. Use `needs_changes` with `workspace_path` and `blocker_reason` for
already-authorized task-scoped PR/branch correction. New scope or effects need
their applicable authority. Use
`needs_user_action` with `blocker_reason` for external blockers.

Only the deterministic PR watcher resets invalid cursors to `uninitialized`
with `pull_request_feedback_invalid`."""


def ci_recovery_prompt(prompt: str) -> str:
    """Adapt only envelope-bearing recovery entries, not real-report routes."""
    prompt = prompt.replace(
        "{{.Params.ci_report}}",
        "(read ci_report only when present in the validated recovery context)",
    )
    return prompt + """

Recovery delivery context: {{.Params.delivery_context}}
Validate the closed delivery_context with the runtime adapter and compare every
supplied flat PR/CI field with its nested value before acting. A missing
ci_report means no report has been produced, not green CI or a task failure.
Preserve absent packets as absent. Recovery, attention and deterministic
preparation remain available without a report; do not invent a report or ask
for human approval merely because the first observation has not happened.
For needs_user_action preserve this context. For report-required routes use
the qualified delivery packer in preserve mode with original producer_refs;
it extracts only an existing report with exact bytes. Green/watch_merge
requires real current evidence; confirmed merge still uses the existing
merge-proof route. Preserve acknowledged feedback and inherited retry limits.
"""


def ci_prompt(profile: ProjectProfile) -> str:
    source_ci = profile.source_ci_contract() and profile.capability("ci_monitoring")
    source_ci_v3 = source_ci and profile.source_ci_contract_v3()
    delivery_fix_handoff = (
        """\n\nFor `needs_changes`, pack the latest observed PR identity, cursor,
and any produced CI packets in `delivery_context`; preserve packet bytes and
omit unproduced fields."""
        + (
            "\nFor `needs_user_action`, preserve the validated `delivery_context` "
            "and its optional packets; no report is required."
            if source_ci else
            "\nKeep `needs_user_action` on its existing flat self-recovery contract."
        )
        if _delivery_context_enabled(profile)
        else ""
    )
    retry_output = (
        "`workspace_path`, `task_short_id={{.TaskShortId}}`, and "
        "`delivery_context`"
        if source_ci
        else "`workspace_path`, `pr_url`, `branch_name`, and\n"
        "`merge_strategy` plus the unchanged accumulated `ci_report`"
    )
    cursor = (
        "\nIncremental PR feedback cursor: {{.Params.pr_feedback_cursor}}\n"
        if profile.runtime_contracts_v2()
        else ""
    )
    source_cycle = (
        """In source-contract mode, the CI contract contains only the current
PR identity and whether CI is required. It contains no expected job names or
release fields. The deterministic watcher classifies every effective check
reported for that PR; failed extras are not ignored and pending observations
are not human actions. When a retry is allowed, choose `watch_ci`; preserve
the current identity, any produced contract/report, and cursor in the packet.
Do not choose `waiting_pr` directly."""
        if source_ci
        else ""
    )
    source_values = (
        """GitHub CI contract: {{.Params.ci_contract}}"""
        + (
            "\nTask: {{.Params.task_short_id}}"
            if source_ci
            else ""
        )
        if profile.capability("ci_monitoring")
        else ""
    )
    preparation_diagnosis = (
        """A `ci_prepare_failed` entry is preparation diagnosis, not a terminal
CI observation. Missing packets are represented by empty strings only on this
diagnostic route. Never parse an empty `ci_report` as a report, invent a
contract, or infer green CI. Preserve the supplied contract, report, cursor,
and the exact PR identity in `delivery_context` when choosing `watch_ci`.
Supply `task_short_id` as its own transition parameter. This always re-enters
deterministic preparation, never the watcher directly. Only a bounded retry
or a proven task-scoped fix/external blocker is allowed."""
        if source_ci and not source_ci_v3
        else ""
    )
    target_branch = (
        "\nPR target branch (`baseRefName`): {{.Params.pr_base_branch}}\n"
        if _delivery_target_family(profile)
        else ""
    )
    return f"""Monitor CI for {{{{.TaskShortId}}}} without editing files.

{context_instruction(profile, "delivery", "ci_monitor", "delivery")}{delivery_context_pack_instruction(profile)}PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
{target_branch}
Resolved merge strategy: {{{{.Params.merge_strategy}}}}
Workspace: {{{{.Params.workspace_path}}}}
Deterministic CI report: {{{{.Params.ci_report}}}}
{cursor}

{procedure_instruction(profile, "ci")}
{source_cycle}
{source_values}
{preparation_diagnosis}

Apply the `ci-monitor` role contract to the exact terminal watcher report when present.
Do not start another polling loop or ask for approval merely to wait. Re-read
only the exact PR/run/job metadata and bounded failed-job logs. If the role's
bounded exact-job retry policy applies, perform one permitted retry and choose
`watch_ci` with {retry_output}; the deterministic
watcher owns the wait and appends the new terminal observation. Preserve every
attempt and failure fingerprint in `ci_report`.

Query authoritative PR merge state before classifying any failed or late check.
First perform bounded baseline/task/target attribution using exact failure
evidence. Unknown attribution is neither an external blocker nor permission
to edit: retain the concrete gap in the existing diagnosis route. Escalate
only an established access/service/authority or decision obstacle; preserve
retry limits. Prior-step budgets do not exclude proven in-scope regressions.
If the PR is already merged, never route the merged task branch to Fix. Complete
with `pr_merged` and provide `workspace_path`, `pr_url`, `branch_name`, and a
`merge_report` that includes merge proof plus the late CI state. Any actionable
post-merge regression belongs in a separate follow-up task.

{("In source-contract mode there is no direct `waiting_pr` transition; the "
  "deterministic watcher must prove green after preparation."
  if profile.source_ci_contract() else
  "While the PR remains open, complete with `waiting_pr` only when all required "
  "checks are green and the resolved method remains feasible. Provide")}
`workspace_path`, `pr_url`, `branch_name`, `merge_strategy`, and `ci_report`.
Use `needs_changes` with `workspace_path` and `fix_context` for a proven
task-differential failure or normal target conflict; no task-differential proof
is needed for that integration conflict. Fix re-verifies the exact PR
head/base. After retry exhaustion, `needs_user_action` is only for an
external blocker.{delivery_fix_handoff}
Never use `needs_user_action` merely because CI is still running."""


def ci_preparation_failure_prompt(profile: ProjectProfile) -> str:
    """Handle v3 preparation failure without pretending CI packets exist."""
    return f"""Classify the failed CI preparation for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "ci_monitor", "delivery")}
Workspace: {{{{.Params.workspace_path}}}}
Task: {{{{.Params.task_short_id}}}}
Closed preparation diagnostic: {{{{.Params.ci_preparation_diagnostic}}}}

This route carries exactly `workspace_path`, `task_short_id`, and
`ci_preparation_diagnostic`; it does not contain a CI report, CI contract, or
flat PR identity. Validate the diagnostic as
`workflow-ci-preparation-diagnostic-v1`. Do not invent or reconstruct any
omitted packet.

For a transient failure or an archive blocker that may have cleared, choose
`retry_preparation` only when the diagnostic contains its validated
`resume_context`. Pass that exact compact JSON string as `delivery_context`,
with `workspace_path` and `task_short_id={{.TaskShortId}}`; do not reset
history, authority, or retry limits. Never route this preparation failure
directly to the CI watcher.

For invalid input, identity conflict, an unavailable original context, or a
genuine external/authority decision, choose `preparation_needs_user_action`
with the unchanged diagnostic and a precise `blocker_reason`. Approval does not
repair contradictory evidence. If the diagnostic does not support either
declared route, preserve the blocker and do not fabricate context."""


def ci_preparation_user_action_prompt(profile: ProjectProfile) -> str:
    return f"""Resolve the human-action blocker from CI preparation for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "ci_monitor", "delivery")}
Workspace: {{{{.Params.workspace_path}}}}
Task: {{{{.Params.task_short_id}}}}
Original closed preparation diagnostic:
{{{{.Params.ci_preparation_diagnostic}}}}
Blocker: {{{{.Params.blocker_reason}}}}

Re-read the exact original input and decision/evidence sources before taking
any action. Preserve the diagnostic bytes and original PR/CI context; do not
invent missing packets, discard invalid supplied packets, or reset retry
history. Resume only through the declared `retry_preparation` route after the
blocker is genuinely resolved and the diagnostic still validates. Otherwise
remain blocked with the precise external or authority gap; approval alone does
not repair technical or contradictory evidence."""


def waiting_pr_prompt(profile: ProjectProfile) -> str:
    source_ci = profile.source_ci_contract() and profile.capability("ci_monitoring")
    delivery_fix_handoff = (
        """\n\nFor `needs_changes`, pack the latest observed identity, cursor, and
any produced CI packets in `delivery_context`; preserve packet strings exactly
and omit unproduced fields."""
        + (
            "\nFor source-contract `ci_required`, provide only "
            "`workspace_path`, `task_short_id={{.TaskShortId}}`, and the updated "
            "`delivery_context`; the producer unwraps those observed values. "
            "For `needs_user_action`, preserve the validated `delivery_context` "
            "and only actually produced CI packets."
            if source_ci
            else "\nKeep non-source-contract `ci_required` and "
            "`needs_user_action` on their existing flat contracts."
        )
        if _delivery_context_enabled(profile)
        else ""
    )
    cursor = (
        "\nIncremental PR feedback cursor: {{.Params.pr_feedback_cursor}}\n"
        if profile.runtime_contracts_v2()
        else ""
    )
    target_branch = (
        "\nPR target branch (`baseRefName`): {{.Params.pr_base_branch}}\n"
        if _delivery_target_family(profile)
        else ""
    )
    ci_recheck = (
        """If the PR head changed or required checks restarted, choose
`ci_required` and provide `workspace_path`, `task_short_id={{.TaskShortId}}`,
and `delivery_context`; the producer will unwrap the exact current PR state."""
        if source_ci
        else """If the PR head changed or required checks restarted, choose
`ci_required` and provide `workspace_path`, `pr_url`, `branch_name`, and
`merge_strategy`; the CI node will watch the new exact run."""
        if profile.capability("ci_monitoring")
        else """If the PR head changed, revalidate the available checks and
method in this node before starting another deterministic merge watch."""
    )
    source_contract = (
        """GitHub CI contract:
{{.Params.ci_contract}}
CI report: {{.Params.ci_report}}
The contract binds only the current PR identity and whether CI is required.
The deterministic watcher considers every effective check it observes; no
expected job list, digest, or extra-check allowlist can make a failure
irrelevant. Preserve the contract and CI report through every watcher
re-entry."""
        + (
            "\nTask: {{.Params.task_short_id}}"
            if profile.source_ci_contract()
            else ""
        )
        if profile.capability("ci_monitoring")
        else ""
    )
    return f"""Check delivery state for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "waiting_pr", "delivery")}{delivery_context_pack_instruction(profile)}PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
{target_branch}
Resolved merge strategy: {{{{.Params.merge_strategy}}}}
Workspace: {{{{.Params.workspace_path}}}}
{cursor}

{procedure_instruction(profile, "waiting_pr")}
{source_contract}

Apply the `ci-monitor` role contract. Do not merge or push. Revalidate the
resolved method using method-specific evidence; investigate contradictory
source-control or user-reported feasibility without mutating the task branch.

{ci_recheck}

Choose `pr_merged` only when the source-control system conclusively reports the
PR as merged; provide `workspace_path`, `pr_url`, `branch_name`, and
`merge_report`. If it remains open and the target reports a normal integration
conflict, choose `needs_changes` with the current `pr_report`; do not demand
proof that the task introduced the target change. The existing Fix path owns
conflict resolution and refreshed verification, without granting new push or
force-push rights. Otherwise, when CI is green, no changes are requested, and
the selected method is feasible, choose `watch_merge` and provide
`workspace_path`, `pr_url`, `branch_name`, `merge_strategy`, and the exact
current head and base commits as `pr_head_oid` and `pr_base_oid`. The
deterministic watcher waits without an approval or model turn and wakes this
node only after meaningful state changes. A base OID change wakes this node so
method-specific feasibility can be revalidated through the normal merge-policy
contract.

Use `needs_user_action` only for authentication/access failure, ambiguous or
contradictory merge policy, or another actual human decision.
Merely waiting for review or merge is not a blocker. Use
`needs_changes` with exactly its graph-declared fields when task code or
history must change. Choose `close_without_merge` only when the
latest user comment explicitly approves closing or canceling this PR; provide
`workspace_path`, `pr_report`, and `closure_reason`.{delivery_fix_handoff}"""


def waiting_pr_changed_prompt(profile: ProjectProfile) -> str:
    return (
        waiting_pr_prompt(profile)
        + """

The deterministic watcher stopped because PR state changed:
{{.Params.pr_report}}
"""
        + (
            "\nValidated PR feedback cursor: {{.Params.pr_feedback_cursor}}\n"
            if profile.runtime_contracts_v2()
            else ""
        )
        + """

Classify only that fresh state. The report may include
`observed_feedback_cursor` for newly reported or edited feedback. Keep the
incoming acknowledged cursor unchanged while considering all simultaneous CI,
feedback, head, base, and mergeability changes. Advance the cursor only after
every reported feedback item has been considered. Do not repeat passive
polling in the agent."""
    )


def cleanup_result_instruction() -> str:
    return """Consume completed output; distinguish success, failure, truncation,
and a pending process with an observer handle. Repeat a successful identical
read only for changed state, missing facts or required fresh safety evidence.
On no progress, diagnose the specific missing fact once; completed reads do
not prove a hung shell. Preserve ownership/preflight checks; no new signalling
or monitoring authority is granted."""


def managed_cleanup_coordination_instruction(
    profile: ProjectProfile,
) -> str:
    if not profile.capability("managed_worktrees"):
        return ""
    return """Before terminal preparation, retry or leave, follow the project
Cleanup procedure and the bounded ownership/coordination contract in
`contracts/worktree-contract.md`, retaining applicable attempt limits.
Freeze the exact task root, branch, owner and resource disposition. Close only
safe children proven task-owned; never stop or signal another Session.
Require fresh owner acknowledgement and readback, not Task movement, old PID
exit or occupancy as release proof. Recheck before handoff; stale or incomplete
proof preserves the root and uses the existing recovery route."""


def cleanup_prompt(
    profile: ProjectProfile,
    *,
    merged: bool = False,
    no_pr: bool = False,
    closed: bool = False,
) -> str:
    workspace = "Workspace: {{.Params.workspace_path}}"
    if merged:
        cleanup_mode = "merged"
        context = """PR: {{.Params.pr_url}}
Branch: {{.Params.branch_name}}
Merge proof: {{.Params.merge_report}}"""
    elif no_pr:
        cleanup_mode = "no_pr"
        workspace = "Use the current task execution root as the workspace."
        context = "PR not applicable: {{.Params.pr_report}}"
    elif closed:
        cleanup_mode = "closed_without_merge"
        context = """PR closure report: {{.Params.pr_report}}
Closure reason: {{.Params.closure_reason}}"""
    else:
        cleanup_mode = "report_only"
        context = "Delivery context: {{.Params.review_context}}"
    if profile.capability("managed_worktrees"):
        completion = f"""Do not remove the managed worktree or local branch
inside this agent session. The following deterministic Task Janitor runs only
after this resource-owning Cleanup session exits. It may remove an exact
task-owned clean managed worktree and local branch when recoverability is
proven. For `merged`, it may also delete the same-repository remote task branch
only when GitHub reports the exact branch head in the merged PR and the remote
head has not changed.

Before leaving, preserve the canonical task worktree root as `workspace_path`
and capture `branch_name` with
`git -C <workspace_path> branch --show-current`. Keep both exact values after
leaving; never look up the branch from the post-leave working directory.

Before `run_janitor`, close or terminate every task-owned background shell or
kept-open tool session, then run `kent worktree leave` from this Cleanup
session. Treat a failed leave request as an infrastructure blocker. The
Janitor will refuse deletion while this session still targets the task
worktree.

Complete with `run_janitor` and provide:

- canonical `workspace_path` for the preserved task worktree root;
- `task_short_id` as `{{{{.TaskShortId}}}}`;
- `branch_name` as the exact non-empty output of
  `git -C <workspace_path> branch --show-current`, including `no_pr` and
  `report_only`; never use `null`, `none`, `not-applicable`, an empty value, or
  the Kent task ID by inference;
- `pr_url` and `merge_report`, using the literal `not-applicable` when absent;
- `cleanup_mode` as `{cleanup_mode}`;
- `cleanup_session_id` from the current `KENT_SESSION_ID`;
- a non-empty `cleanup_report` describing preflight and preserved resources,
  including the exact `workspace_path` and `branch_name`.

Use `needs_user_action` only when even conservative preservation requires a
human decision."""
    else:
        completion = """Complete with `done` and provide `cleanup_report`
describing performed and skipped actions. Use `needs_user_action` with
`blocker_reason` when safe cleanup requires a human decision."""
    coordination = managed_cleanup_coordination_instruction(profile)
    coordination_section = f"\n\n{coordination}" if coordination else ""
    return f"""Perform conservative cleanup for {{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "cleanup", "delivery")}

{workspace}
{context}

{procedure_instruction(profile, "cleanup")}

{cleanup_result_instruction()}{coordination_section}

Treat cleanup as report-first. Never delete the primary checkout, dirty or
ambiguous state, or content not proven recoverable.

{completion}"""


def package_publish_prompt(profile: ProjectProfile) -> str:
    return f"""Publish the package for {{{{.TaskShortId}}}} only after this
incoming approval.

{context_instruction(profile, "delivery", "publish_package", "delivery")}

This dedicated node is the separate explicit user-approved release procedure
required by the project contract. Approval authorizes only the exact package
identity, version, destination, merged source, and tag policy declared in the
human-authored task body or an exact human-authored task comment. It does not
authorize source edits, another version, branch pushes, package deletion, or
any unrelated release action.

Task body:
{{{{.TaskBody}}}}

Merged delivery context:
- Workspace: {{{{.Params.workspace_path}}}}
- PR: {{{{.Params.pr_url}}}}
- Branch: {{{{.Params.branch_name}}}}
- Merge proof: {{{{.Params.merge_report}}}}

{procedure_instruction(profile, "publish")}

Before any remote mutation:

1. Re-read repository instructions, the project contract, and latest human
   task comments.
2. Verify the source-control system reports the PR as merged and resolve the
   exact merged commit.
3. Require explicit publication authority naming the package, exact version,
   version override mechanism, destination, and tag policy. Missing or
   contradictory authority is `needs_user_action`.
4. Use a clean checkout representing the exact merged source tree. Never
   publish unmerged, dirty, or tree-divergent code, and never rewrite the
   retained task worktree to manufacture that state.
5. Validate generated publication metadata before mutation.
6. Resolve the project-declared credential source just in time. Verify its
   principal and required registry access without printing it. Do not rely on
   ambient CLI authentication or inherited credential variables.
7. Check whether the exact remote package version already exists. Never
   overwrite or delete a colliding version.

Run only the project-authorized remote publish command. Never use a local
repository substitute for a required remote publication. Do not expose
credentials in output, task comments, artifacts, or logs. Inject the resolved
credential only into the publish subprocess using the project-declared
environment mapping, then clear it.

After publication, verify the exact expected package versions through the
remote package registry. Record package names, version, merged commit, command
shape with secrets omitted, and verification evidence in
`publication_report`.

Complete with `published` and provide `workspace_path`, `pr_url`,
`branch_name`, `merge_report`, and `publication_report`. Complete with
`needs_user_action` for authentication failure, version collision, partial
publication, source mismatch, missing authority, or unverifiable remote state;
preserve the same delivery context and provide `blocker_reason`. A retry must
re-check remote state before another publish attempt."""


def package_publish_recovery_prompt(profile: ProjectProfile) -> str:
    return f"""Resume the approval-gated package publication for
{{{{.TaskShortId}}}}.

{context_instruction(profile, "delivery", "publish_package", "delivery")}

Previous blocker: {{{{.Params.blocker_reason}}}}
Workspace: {{{{.Params.workspace_path}}}}
PR: {{{{.Params.pr_url}}}}
Branch: {{{{.Params.branch_name}}}}
Merge proof: {{{{.Params.merge_report}}}}

Re-read the task body, latest human comments, repository instructions, and the
project contract.

{procedure_instruction(profile, "publish")}

Approval authorizes a fresh remote-state check and, only when safe, one attempt
to publish the exact already-approved package identity. It does not authorize
another version, source edits, tags beyond the declared policy, package
deletion, or overwrite.

Before any publish command, re-resolve the project-declared credential source,
verify its principal and registry access without exposing it, and do not
substitute ambient CLI authentication. Inspect the remote package registry
again for partial or completed publication and verify the clean merged source
identity.
If the package is already completely present with task-owned proof, complete as
`published` without republishing. If state is partial, conflicting, or
unverifiable, return `needs_user_action` again. Never hide a partial remote
mutation. Inject the credential only into the authorized publish subprocess
and clear it afterward.

On success, provide `workspace_path`, `pr_url`, `branch_name`, `merge_report`,
and `publication_report`. On another blocker, preserve the same delivery
context and provide `blocker_reason`."""


def published_cleanup_prompt(profile: ProjectProfile) -> str:
    return cleanup_prompt(profile, merged=True) + """

Publication proof: {{.Params.publication_report}}
Cleanup is allowed only after a non-empty publication report proves that the
exact task-authorized package version exists remotely. Retain that proof in
the cleanup report and, for Janitor, the merged report. Do not publish, tag,
push, or edit project files from Cleanup."""


def janitor_recovery_prompt(profile: ProjectProfile) -> str:
    coordination = managed_cleanup_coordination_instruction(profile)
    coordination_section = f"\n\n{coordination}" if coordination else ""
    return f"""Recover the task cleanup after deterministic Janitor failure.

{context_instruction(profile, "delivery", "cleanup", "delivery")}

Previous cleanup report:
{{{{.Params.cleanup_report}}}}
Blocker:
{{{{.Params.blocker_reason}}}}

{procedure_instruction(profile, "cleanup")}{coordination_section}

Use the retained Cleanup context and the exact `workspace_path` and
`branch_name` recorded in its `cleanup_report`; do not infer either from this
recovery session's current directory. If a branch lookup is needed, use
`git -C <workspace_path> branch --show-current` and require the same exact
branch. Do not directly remove a Kent-managed worktree from this agent session.
Close every task-owned background shell or kept-open tool session. If this
session still targets the task worktree, run `kent worktree leave` before
retrying. If the infrastructure failure is transient and the same safety
proofs still hold, choose `run_janitor` again with the complete canonical
parameter contract. Otherwise choose `needs_user_action` with the exact
blocker. Preserve every ambiguous or unique resource."""


def pr_recovery_fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    delivery_input = delivery_context_input(profile)
    delivery_continuity = delivery_context_tail(profile)
    fresh_contract = ""
    completion_contract = """After resolving task-scoped code, complete with
`verify` and provide `workspace_path`, `task_short_id={{.TaskShortId}}`, plus
refreshed `review_context`."""
    if bounded:
        fresh_contract = """

This is a fresh bounded writer session. Inspect the preserved diff, task
comments, authoritative artifacts, and existing evidence before editing.
Resolve exactly one independently verifiable PR or branch recovery slice."""
        completion_contract = """After one slice, choose `continue_fix` with
`workspace_path`, `task_short_id={{.TaskShortId}}`, and `fix_context`
containing only the remaining task-scoped issues. Choose `verify` only when no
recovery slice remains, and provide `workspace_path`, the same
`task_short_id`, plus refreshed `review_context`."""

    return f"""Resolve an already-authorized task-scoped PR or branch recovery issue.

{context_instruction(profile, "implement", "fix", "implementation")}

Workspace: {{{{.Params.workspace_path}}}}.{delivery_input}Recovery issue: {{{{.Params.blocker_reason}}}}

{procedure_instruction(profile, "fix")}
{fresh_contract}

Existing authority covers only the reported task-scoped PR/branch correction.
Never infer new scope, push rights, a broader rebase, or force-push permission.
{completion_contract}
Use `needs_user_action` with `blocker_reason` if the approved recovery is still
unsafe. Choose `wont_do` only for an explicit cancellation decision and
provide `closure_reason`.{delivery_continuity}"""


def pr_feedback_fix_prompt(
    profile: ProjectProfile,
    *,
    bounded: bool = False,
) -> str:
    context_enabled = _delivery_context_enabled(profile)
    delivery_input = delivery_context_input(profile)
    delivery_continuity = delivery_context_tail(profile)
    pr_identity = (
        """The PR URL, branch, merge strategy, and feedback cursor are in the
lifecycle packet above; preserve and use those exact values.
PR report: {{.Params.pr_report}}"""
        if context_enabled
        else """Resolved merge strategy: {{.Params.merge_strategy}}.
PR report: {{.Params.pr_report}}"""
    )
    fresh_contract = ""
    completion_contract = """Complete with `verify` and provide
`workspace_path`, `task_short_id={{.TaskShortId}}`, plus refreshed
`review_context`."""
    if bounded:
        fresh_contract = """

This is a fresh bounded writer session. Inspect the preserved diff, task
comments, authoritative artifacts, and existing evidence before editing.
Resolve exactly one independently verifiable PR-feedback slice."""
        completion_contract = """After one slice, choose `continue_fix` with
`workspace_path`, `task_short_id={{.TaskShortId}}`, and `fix_context`
containing only the remaining task-scoped issues. Choose `verify` only when no
PR-feedback slice remains, and provide `workspace_path`, the same
`task_short_id`, plus refreshed `review_context`."""

    return f"""Fix task-scoped PR feedback or a normal target-branch integration
conflict.

{context_instruction(profile, "implement", "fix", "implementation")}

Workspace: {{{{.Params.workspace_path}}}}.{delivery_input}{pr_identity}

{procedure_instruction(profile, "fix")}
{fresh_contract}

Remain the single writer. Do not merge or push protected branches. A history
rewrite or force-push requires an exact latest user authorization naming the
branch and permitted repair. Preserve the old remote head in a local backup,
pin the expected remote head, prove the repaired final tree is byte-for-byte
identical unless the authorization explicitly permits code changes, and update
only the task branch with force-with-lease. Stop with `needs_user_action` on a
lease, tree, target-tip, or authorization mismatch; never fall back to an
unconditional force push. For an ordinary conflict with the current target
branch, preserve the immutable task baseline and exact PR identity, resolve
the integration conflict through the existing approved merge strategy, and do
not require task-differential proof that the target introduced the conflict.
No new push or force-push authority is granted by this route.
If the PR report includes `observed_feedback_cursor`, consider every reported
feedback item before acknowledging it; retain the acknowledged cursor through
the full Fix and verification loop.
{completion_contract}
Use `needs_user_action` with `blocker_reason` for external or policy blockers.
Choose `wont_do` only for an explicit cancellation decision and provide
`closure_reason`.{delivery_continuity}"""


def post_smoke_target(profile: ProjectProfile) -> str:
    if profile.capability("pull_requests"):
        if profile.capability("compliance_review"):
            return "compliance"
        return "prepare_pr"
    return "cleanup"


def delivery_prompt(profile: ProjectProfile, target: str) -> str:
    if target == "compliance":
        return compliance_prompt(profile)
    if target == "prepare_pr":
        return prepare_pr_prompt(profile)
    if target == "cleanup":
        return cleanup_prompt(profile)
    raise ValueError(f"unsupported delivery target {target!r}")


def smoke_decision_instruction(profile: ProjectProfile) -> str:
    procedure = procedure_instruction(profile, "smoke_decision")
    policy = profile.smoke_policy()
    if policy == "required":
        return f"""Project policy requires runtime Smoke after verification.
{procedure}
Choose `smoke_required` and provide `smoke_rationale` plus a focused
`smoke_scope`. Device, browser, simulator, or hardware unavailability is not a
reason to bypass Smoke; the Smoke node must report `needs_user_action`."""
    if policy == "disabled":
        return """Project policy disables runtime Smoke for this workflow.
Choose `delivery_ready` and set `smoke_rationale` to the profile policy. Do not
invent a Smoke requirement that the project cannot execute."""
    if policy == "conditional":
        return f"""Project policy makes runtime Smoke conditional.
{procedure}
Choose `smoke_required` for user-visible or runtime behavior, navigation, state
or data flow, permissions or security, storage or migrations, external
integration, browser/device/hardware interaction, explicit acceptance criteria,
or uncertain runtime impact. Provide `smoke_rationale` and a focused
`smoke_scope`.

Choose `delivery_ready` only for changes proven not to affect a runtime
artifact or user-observable behavior, and provide an evidence-based
`smoke_rationale`. Uncertainty must route to `smoke_required`. Resource
unavailability must never downgrade the decision."""
    raise ValueError(f"unsupported smoke policy {policy!r}")
