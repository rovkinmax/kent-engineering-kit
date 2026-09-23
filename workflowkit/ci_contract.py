"""Dynamic CI preparation and release-source admission contracts.

Ordinary preparation binds a stable GitHub pull-request identity and creates
the dynamic CI cycle contract. It does not inspect release source jobs,
materialize commits, or infer expected checks. The legacy source-derived
normalizer and admission helpers remain available to release-only callers.
Input is duplicate-free JSON, <=1 MiB/depth 100, read within two seconds.
Workspace, canonical GitHub PR URL, branch, strategy and task short ID are real
nonempty strings. Invalid identity exits nonzero with a safe error, no transition.
Errors never contain source snippets, authenticated output or subprocess stderr.

ci_prepare_failed is preparation diagnosis, NOT a new CI report schema.
It preserves all identity/cursor/packet strings; absent contract/report packets
are empty strings ONLY on diagnosis and its retry back to preparation. Supplied
invalid reports remain invalid. CI Monitor has no positive-green route.
Ready-initial omits the absent report; ready-retry carries a validated same-cycle
github-ci-report-v3. Both carry the contract, task ID and cursor.

P need not adopt the producer. H declares the capability and contains matching
first-party prepare/CI-wait/PR-wait/runtime templates. Metadata matches the exact
PR URL/head branch; re-reading preserves base branch, H and P. Merged recovery
requires timestamp and merge-commit proof. Missing P/H objects are fetched by
full OID from the verified HTTPS repository into the object cache only: no refs,
tags, FETCH_HEAD, hooks, submodules or maintenance. Selected source reads have
a cumulative 60-second/32-MiB budget plus per-command bounds. Blob sizes are
checked before reads; external captures are regular Git blobs <=1 MiB/root,
4 MiB total, with declared digest/order and supported builder/source-sha256 kinds.
Builder keys are digests bound to H's profile.release.builder_path; source keys
are path=digest. Accepted digests are lowercase hex64 or exactly sha256: followed
by eight colon-separated lowercase hex8 groups, matching existing project
manifests. Keys/order are preserved, not rewritten; #/@ delimiters are invalid.
Source reads disable replacement refs, lazy fetch and inherited GIT_* routing/
configuration. Explicit HTTPS cache fetch alone can access the network, using
the already-resolved gh auth git-credential client, not global Git helpers or
rewrites. Helper names may be inspected locally to reset them only in argv;
no credential values or auth configuration are read or written by the producer.

Required source DTOs from P and H are always validated, not inferred from green
checks. Extras remain diagnostic. Aggregate --watch and branch-rule --required
are not used in source mode; bounded observations are classified against the
source-required identities. Required-job effective semantics (not whole release
fields or only names) determine policy drift. Same H/envelope/effective
policy preserves report attempts even when P advances. Changed H/policy starts
a new report, retaining old reports in the existing task evidence area.

The archive_ci_report CLI operation validates identity/report and returns only
ci_report_artifact, or a nonzero safe error. Artifacts are content-addressed
ci-report-<sha256>.json files under .kent/runtime/<task>, <=64 reports of <=64 KiB,
with an existing evidence-ledger entry, no new journal or policy authority store.
The ledger must be ignored, unsealed and safe; failures cannot yield green CI.
Terminal CI observations archive before transition; preparation retains prior
reports before retry/new-cycle transitions. Archive reads are bounded to 1 MiB
of existing ledger/256 task-directory entries. Runtime validation remains pure.

Supported runner assertion: unconditional, non-continue-on-error, standalone
POSIX test in a default/sh/bash step, using either $RUNNER_ENVIRONMENT or the
existing Puber ${RUNNER_ENVIRONMENT:-github-hosted} convention. Exact quoted
spellings are below. Substrings, conditional steps, masks and other shells do
not assert runner trust. Empty shell remains empty in the normalized DTO.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import selectors
import shlex
import signal
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlparse

from .profile import ProjectProfile
from .release import (
    NormalizedGitHubWorkflowSourceV1,
    ReleaseSpec,
    ReleaseSpecError,
    validate_required_job_sources,
)
from .revision import (
    RevisionPreflightError,
    collect_runtime_external_captures,
    preflight_project_revision,
    read_blob_bytes,
    source_read_budget,
    selected_git_command,
    selected_git_environment,
    owned_process_group_exists,
)
from .runtime import (
    RuntimeContractError,
    capture_runtime_source_envelope,
    canonical_bytes,
    validate_ci_contract,
    validate_dynamic_ci_report,
    validate_dynamic_pr_feedback_cursor,
    make_ci_policy_snapshot,
    validate_ci_policy_snapshot,
    validate_ci_report,
    validate_expected_ci_checks,
    expected_ci_checks_sha256,
    ci_policy_projection_sha256,
    MAX_CI_REPORT_BYTES,
)


MAX_GITHUB_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_WORKFLOW_BYTES = 1024 * 1024
MAX_RUBY_OUTPUT_BYTES = 2 * 1024 * 1024
PREPARE_TIMEOUT_SECONDS = 60.0
MAX_PREPARATION_INPUT_BYTES = 1024 * 1024
SHA1_HEX = set("0123456789abcdef")


class CiContractError(RuntimeError):
    """Raised when CI preparation or source admission cannot prove its contract."""


class BoundedCommandError(CiContractError):
    def __init__(
        self,
        code: str,
        *,
        exit_code: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_preparation_json(raw: bytes | str) -> Any:
    """Bounded duplicate-free JSON; stdin need not be canonical."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_PREPARATION_INPUT_BYTES:
        raise CiContractError("input_limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CiContractError("input_invalid")
            result[key] = value
        return result

    def constant(_):
        raise CiContractError("input_invalid")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        canonical_bytes(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CiContractError("input_invalid") from error


def read_preparation_input(stream) -> dict[str, Any]:
    deadline = time.monotonic() + 2.0
    chunks = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise CiContractError("input_timeout")
        chunk = os.read(stream.fileno(), min(65536, MAX_PREPARATION_INPUT_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_PREPARATION_INPUT_BYTES:
            raise CiContractError("input_limit")
    value = parse_preparation_json(bytes(chunks))
    if not isinstance(value, dict):
        raise CiContractError("input_invalid")
    return value


def validate_preparation_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    identity = {}
    for key in ("workspace_path", "pr_url", "branch_name", "merge_strategy", "task_short_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value or value != value.strip():
            raise CiContractError("input_identity_invalid")
        if len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise CiContractError("input_identity_invalid")
        identity[key] = value
    _repository_from_url(identity["pr_url"])
    if (
        not Path(identity["workspace_path"]).is_absolute()
        or identity["merge_strategy"] not in {"merge", "squash", "rebase"}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity["task_short_id"])
        or identity["branch_name"].startswith("-")
    ):
        raise CiContractError("input_identity_invalid")
    return identity


def preparation_failure(payload: Mapping[str, Any]) -> dict[str, str]:
    """Diagnosis transport only: empty strings denote absent packets, not reports."""
    identity = validate_preparation_identity(payload)
    values = {}
    for key in ("ci_contract", "ci_report"):
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise CiContractError("input_invalid")
        values[key] = value
    cursor = payload.get("pr_feedback_cursor", "uninitialized")
    if not isinstance(cursor, str):
        raise CiContractError("input_invalid")
    return {
        "transition": "ci_prepare_failed", **identity, **values,
        "pr_feedback_cursor": cursor,
    }


def _terminate(
    process: subprocess.Popen[bytes],
    process_group_id: int,
) -> None:
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    # The leader may exit on TERM while a descendant ignores it and retains
    # our pipe. Cleanup owns the entire newly-created session, not just Popen.
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_bounded_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    stdin: bytes = b"",
    timeout: float = PREPARE_TIMEOUT_SECONDS,
    output_limit: int = MAX_GITHUB_OUTPUT_BYTES,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process_group_id = process.pid
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdin.fileno(), False)
    if stdin:
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    input_offset = 0
    deadline = time.monotonic() + timeout
    failure: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "github_query_failed"
                break
            events = selector.select(remaining)
            if not events:
                failure = "github_query_failed"
                break
            for key, _ in events:
                if key.data == "stdin":
                    try:
                        written = os.write(
                            key.fileobj.fileno(),
                            stdin[input_offset:],
                        )
                    except (BrokenPipeError, OSError) as error:
                        failure = "github_query_failed"
                        break
                    input_offset += written
                    if input_offset == len(stdin):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buffer = output[key.data]
                if len(buffer) + len(data) > output_limit:
                    failure = "hard_limit"
                    break
                buffer.extend(data)
            if failure:
                break
    except BaseException:
        _terminate(process, process_group_id)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        raise
    finally:
        selector.close()
    if failure:
        _terminate(process, process_group_id)
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdout.close()
        process.stderr.close()
        raise BoundedCommandError(
            failure,
            stdout=bytes(output["stdout"]),
            stderr=bytes(output["stderr"]),
        )
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate(process, process_group_id)
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdout.close()
        process.stderr.close()
        raise BoundedCommandError(
            "github_query_failed",
            stdout=bytes(output["stdout"]),
            stderr=bytes(output["stderr"]),
        )
    stdout = bytes(output["stdout"])
    stderr = bytes(output["stderr"])
    process.stdout.close()
    process.stderr.close()
    if owned_process_group_exists(process_group_id):
        _terminate(process, process_group_id)
        raise BoundedCommandError(
            "github_query_failed", stdout=stdout, stderr=stderr,
        )
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )


def _github_bin() -> str:
    configured = os.environ.get("KENT_GH_BIN")
    candidates = (configured,) if configured else ("gh",)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        if candidate:
            from shutil import which

            resolved = which(candidate)
            if resolved:
                return resolved
    raise CiContractError("GitHub CLI was not found")


def read_pull_request(
    workspace: Path,
    pr_url: str,
    *,
    gh_bin: str | None = None,
) -> dict[str, Any]:
    result = run_bounded_command(
        [
            gh_bin or _github_bin(),
            "pr",
            "view",
            pr_url,
            "--json",
            "state,headRefOid,baseRefOid,baseRefName,headRefName,url,mergedAt,mergeCommit",
        ],
        cwd=workspace,
    )
    if result.returncode != 0:
        raise BoundedCommandError(
            "github_query_failed",
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    try:
        value = parse_preparation_json(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CiContractError("GitHub PR metadata is not valid JSON") from error
    if not isinstance(value, dict):
        raise CiContractError("GitHub PR metadata must be an object")
    return value


def _repository_from_url(pr_url: str) -> str:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query or parsed.fragment or parsed.params
        or len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
        or int(parts[3]) <= 0
        or parsed.path != "/" + "/".join(parts)
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2])
    ):
        raise CiContractError("pr_url must identify one GitHub pull request")
    return f"{parts[0]}/{parts[1]}"


def _validate_pr_metadata(pr: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    if (
        pr.get("url") != payload["pr_url"]
        or pr.get("headRefName") != payload["branch_name"]
        or pr.get("state") not in {"OPEN", "MERGED"}
        or not isinstance(pr.get("baseRefName"), str)
        or not pr["baseRefName"]
    ):
        raise CiContractError("pull_request_identity_invalid")
    for key in ("headRefOid", "baseRefOid"):
        if not isinstance(pr.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", pr[key]):
            raise CiContractError("pull_request_identity_invalid")
    if pr["state"] == "MERGED":
        proof = pr.get("mergeCommit")
        if (
            not isinstance(pr.get("mergedAt"), str)
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", pr["mergedAt"])
            or not isinstance(proof, dict)
            or not isinstance(proof.get("oid"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", proof["oid"])
        ):
            raise CiContractError("merged_proof_missing")
    elif pr.get("mergedAt") or pr.get("mergeCommit"):
        raise CiContractError("pull_request_identity_invalid")


def materialize_exact_commit(
    root: Path, repository: str, oid: str, *, gh_bin: str | None = None,
) -> None:
    """Fetch only a verified-repository exact OID into the existing object cache."""
    if not re.fullmatch(r"[0-9a-f]{40}", oid) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository,
    ):
        raise CiContractError("source_identity_invalid")
    env = selected_git_environment()
    prefix = selected_git_command(root)
    present = run_bounded_command(
        [*prefix, "cat-file", "-t", oid], env=env, output_limit=4096,
    )
    if present.returncode == 0:
        if present.stdout.strip() != b"commit":
            raise CiContractError("source_identity_invalid")
        return
    # Repository-local rewriting would defeat the fixed HTTPS authority.
    rewrites = run_bounded_command(
        [*prefix, "config", "--local", "--get-regexp", r"^url\..*\.insteadof$"],
        env=env, output_limit=4096,
    )
    if rewrites.returncode != 1:
        raise CiContractError("source_fetch_configuration_invalid")
    # Do not re-enable global configuration (which can rewrite repository
    # routing). Use the same resolved first-party GitHub authentication client
    # as metadata queries. Git exchanges credentials with gh directly; tokens
    # never enter Python, argv, logs or persisted configuration.
    configured_helpers = run_bounded_command(
        [*prefix, "config", "--local", "--name-only", "--get-regexp", r"^credential(\..*)?\.helper$"],
        env=env, output_limit=4096,
    )
    if configured_helpers.returncode not in (0, 1):
        raise CiContractError("source_fetch_configuration_invalid")
    resets = ["-c", "credential.helper="]
    for key in configured_helpers.stdout.decode("utf-8").splitlines():
        if not key.startswith("credential.") or not key.endswith(".helper") or any(ord(c) < 32 for c in key):
            raise CiContractError("source_fetch_configuration_invalid")
        resets.extend(["-c", key + "="])
    client = str(Path(gh_bin or _github_bin()).resolve())
    helper = "!" + shlex.quote(client) + " auth git-credential"
    url = f"https://github.com/{repository}.git"
    credential_key = f"credential.{url}.helper"
    result = run_bounded_command(
        [
            *prefix, *resets,
            "-c", credential_key + "=", "-c", credential_key + "=" + helper,
            "-c", "credential.interactive=false", "-c", "credential.useHttpPath=true",
            "-c", "http.followRedirects=false",
            "-c", f"core.hooksPath={os.devnull}",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0",
            "-c", "fetch.writeCommitGraph=false", "-c", "submodule.recurse=false",
            "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "fetch", "--no-write-fetch-head", "--no-tags",
            "--no-recurse-submodules", "--no-auto-maintenance",
            url, oid,
        ],
        env=env,
    )
    if result.returncode:
        raise CiContractError("source_fetch_failed")
    verified = run_bounded_command(
        [*prefix, "cat-file", "-t", oid], env=env, output_limit=4096,
    )
    if verified.returncode or verified.stdout.strip() != b"commit":
        raise CiContractError("source_fetch_failed")


def _validate_adoption(root: Path, head: str, profile: ProjectProfile) -> None:
    if not profile.source_ci_contract():
        raise CiContractError("source_adoption_incomplete")
    kit = Path(__file__).resolve().parents[1]
    sources = {
        "prepare_ci": kit / "templates/project/workflow-prepare-github-ci",
        "wait_ci": kit / "templates/project/workflow-wait-github-ci",
        "wait_pr": kit / "templates/project/workflow-wait-github-pr",
        "runtime_contracts": kit / "workflowkit/runtime.py",
    }
    for capability, template in sources.items():
        observed = read_blob_bytes(
            root, head, profile.command(capability), label="source CI adapter",
        )
        if observed != template.read_bytes():
            raise CiContractError("source_adoption_incomplete")


def archive_ci_report(root: Path, task: str, report: Mapping[str, Any]) -> str:
    """Retain a validated report in the existing locked task evidence area."""
    import fcntl

    normalized = validate_dynamic_ci_report(report)
    raw = canonical_bytes(normalized)
    if len(raw) > MAX_CI_REPORT_BYTES:
        raise CiContractError("report_archive_limit")
    template = Path(__file__).resolve().parents[1] / "templates/project/workflow-evidence-ledger"
    loader = importlib.machinery.SourceFileLoader("_ci_evidence_ledger", str(template))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    ledger = importlib.util.module_from_spec(spec)
    loader.exec_module(ledger)
    path = ledger.ledger_path(root, task)
    ledger.require_ignored(root, path)
    name = f"ci-report-{_sha256(raw)}.json"
    artifact = str(path.parent.relative_to(root) / name)
    descriptors, _, _, _, task_fd = ledger._open_runtime_fds(
        root, task, create_task=True, lock_operation=fcntl.LOCK_EX | fcntl.LOCK_NB,
    )
    try:
        # A bounded artifact set and bounded ledger read are not a new journal.
        names = []
        with os.scandir(task_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 256:
                    raise CiContractError("report_archive_limit")
        if len([item for item in names if item.startswith("ci-report-")]) >= 64 and name not in names:
            raise CiContractError("report_archive_limit")
        try:
            descriptor = ledger._open_runtime_file(task_fd, name, create=False)
        except FileNotFoundError:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=task_fd,
            )
            try:
                with os.fdopen(os.dup(descriptor), "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(task_fd)
            finally:
                os.close(descriptor)
        else:
            try:
                if os.fstat(descriptor).st_size != len(raw) or os.read(descriptor, len(raw) + 1) != raw:
                    raise CiContractError("report_archive_invalid")
            finally:
                os.close(descriptor)
        if "evidence-ledger.jsonl" in names:
            descriptor = ledger._open_runtime_file(task_fd, "evidence-ledger.jsonl", create=False)
            try:
                if os.fstat(descriptor).st_size > MAX_PREPARATION_INPUT_BYTES:
                    raise CiContractError("report_archive_limit")
                with os.fdopen(os.dup(descriptor), encoding="utf-8") as stream:
                    entries = ledger.load_entries(stream, task=task)
                if any(artifact in entry.get("artifacts", []) for entry in entries):
                    return artifact
            finally:
                os.close(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    manifest_path = next(
        (
            relative
            for relative in (".kent/context/delivery.md", ".kent/project-contract.md")
            if (root / relative).is_file()
        ),
        None,
    )
    if manifest_path is None:
        raise CiContractError("report_archive_context_invalid")
    result = run_bounded_command(
        [sys.executable, "-B", str(template), "append", "--task", task, "--workspace", str(root)],
        stdin=canonical_bytes({
            "node_key": "ci_prepare", "evidence_type": "ci_report",
            "summary": "Validated CI cycle report retained before preparation re-entry.",
            "artifacts": [artifact], "checks": ["github-ci-report-v3 validated"],
            "context": {
                "manifest_path": manifest_path,
                "files_read": [], "model_calls": 0, "compaction_count": 0,
            },
        }),
        output_limit=4096,
    )
    if result.returncode:
        raise CiContractError("report_archive_failed")
    receipt = parse_preparation_json(result.stdout)
    if receipt.get("duplicate_suppressed"):
        # The ledger owns run-level idempotency; never claim a different report
        # is archived merely because another event used the same run identity.
        raise CiContractError("report_archive_conflict")
    return artifact


def _ruby_normalizer() -> str:
    return r"""
require "json"
require "yaml"

class StrictHash < Hash
  def []=(key, value)
    raise "json_duplicate_key" if key?(key)
    super
  end
end

def reject(node, depth = 0)
  raise "yaml_depth" if depth > 64
  return unless node
  if node.is_a?(Psych::Nodes::Alias)
    raise "yaml_alias"
  end
  if node.is_a?(Psych::Nodes::Mapping)
    keys = []
    node.children.each_slice(2) do |key, value|
      if key.is_a?(Psych::Nodes::Scalar)
        raise "yaml_duplicate_key" if keys.include?(key.value)
        keys << key.value
      end
      raise "yaml_key" unless key.is_a?(Psych::Nodes::Scalar)
      reject(key, depth + 1)
      reject(value, depth + 1)
    end
  elsif node.respond_to?(:children)
    children = node.children
    children.each { |child| reject(child, depth + 1) } if children.respond_to?(:each)
  end
end

def string_value(value, fallback = "")
  return fallback if value.nil?
  return value if value.is_a?(String)
  raise "string_required"
end

def string_list(value)
  return [] if value.nil?
  value = [value] unless value.is_a?(Array)
  raise "string_list" unless value.all? { |item| item.is_a?(String) }
  raise "duplicate_value" unless value.uniq == value
  value.sort
end

def mapping(value)
  return {} if value.nil?
  raise "mapping_required" unless value.is_a?(Hash)
  raise "mapping_key" unless value.keys.all? { |key| key.is_a?(String) }
  value
end

def boolean(value, fallback = false)
  return fallback if value.nil?
  raise "boolean_required" unless value == true || value == false
  value
end

def condition(value)
  return value.to_s if value == true || value == false
  string_value(value)
end

def permissions(value)
  # Broad shorthand cannot be represented without binding a GitHub permission
  # catalog; fail closed instead of pretending it is an empty/read-only map.
  mapping(value)
end

def defaults(value)
  run = mapping(mapping(value)["run"])
  raise "defaults_key" unless (run.keys - ["shell", "working-directory"]).empty?
  run
end

def service(value)
  value = {"image" => value} if value.is_a?(String)
  value = mapping(value)
  raise "service_key" unless (value.keys - ["image", "env", "ports", "options"]).empty?
  {
    "image" => string_value(value["image"]),
    "environment" => scalar_map(value["env"]),
    "ports" => string_list(value["ports"]),
    "options" => string_value(value["options"])
  }
end

def event_record(name, config)
  config = mapping(config)
  raise "event_key" unless (config.keys - [
    "branches", "branches-ignore", "tags", "tags-ignore", "paths",
    "paths-ignore", "types", "inputs"
  ]).empty?
  inputs = mapping(config["inputs"]).sort.map do |input_name, raw|
    input = mapping(raw)
    raise "input_key" unless (input.keys - [
      "type", "required", "default", "description", "options"
    ]).empty?
    {
      "name" => input_name, "type" => string_value(input["type"], "string"),
      "required" => boolean(input["required"]),
      "default_present" => input.key?("default")
    }.merge(input.key?("default") ? {"default" => input["default"]} : {})
  end
  {
    "name" => name.to_s,
    "branches" => string_list(config["branches"]),
    "branches_ignore" => string_list(config["branches-ignore"]),
    "tags" => string_list(config["tags"]),
    "tags_ignore" => string_list(config["tags-ignore"]),
    "paths" => string_list(config["paths"]),
    "paths_ignore" => string_list(config["paths-ignore"]),
    "types" => string_list(config["types"]),
    "dispatch_inputs" => inputs
  }
end

def scalar_map(value)
  value = mapping(value)
  raise "scalar_map" unless value.values.all? do |v|
    v.is_a?(String) || v.is_a?(Integer) || v == true || v == false
  end
  value
end

def secret_refs(value)
  if value.is_a?(String)
    value.scan(
      /(?:\$\{\{\s*)?secrets\.([A-Za-z_][A-Za-z0-9_-]*)(?:\s*\}\})?/
    ).flatten
  elsif value.is_a?(Hash)
    value.flat_map { |key, item| secret_refs(key) + secret_refs(item) }
  elsif value.is_a?(Array)
    value.flat_map { |item| secret_refs(item) }
  else
    []
  end.uniq.sort
end

raw = STDIN.read
value =
  if ARGV.fetch(0) == "json"
    JSON.parse(raw, object_class: StrictHash)
  else
    stream = Psych.parse_stream(raw)
    raise "yaml_documents" unless stream.children.length == 1
    stream.children.each { |document| reject(document.root) }
    Psych.safe_load(raw, aliases: false)
  end
raise "workflow_object" unless value.is_a?(Hash)
jobs = value.fetch("jobs")
raise "jobs_object" unless jobs.is_a?(Hash)
rows = []
workflow_permissions = permissions(value["permissions"])
workflow_environment = scalar_map(value["env"])
workflow_defaults = defaults(value["defaults"])
raise "ambiguous_on" if value.key?("on") && value.key?(true)
trigger_value = value.key?("on") ? value["on"] : value[true]
workflow_events =
  if trigger_value.is_a?(String)
    [event_record(trigger_value, {})]
  elsif trigger_value.is_a?(Array)
    trigger_value.map { |item| event_record(item, {}) }
  elsif trigger_value.is_a?(Hash)
    trigger_value.map { |event, config| event_record(event, config) }
  else
    raise "workflow_events"
  end
selected = JSON.parse(ARGV.fetch(2))
if selected
  raise "missing_job" unless (selected - jobs.keys).empty?
  jobs = jobs.select { |key, _| selected.include?(key) }
end
jobs.each do |key, job|
  raise "job_key" unless key.is_a?(String) && key.match?(/\A[A-Za-z0-9_.-]+\z/)
  raise "job_object" unless job.is_a?(Hash)
  name = job["name"] || key
  raise "job_name" unless name.is_a?(String) && !name.strip.empty?
  raise "job_expression" if name.include?("${{") || key.include?("${{")
  strategy = mapping(job["strategy"])
  matrix = strategy["matrix"]
  raise "unsupported_matrix" unless matrix.nil? || matrix == {}
  needs = string_list(job["needs"])
  if selected && !job.key?("permissions") && !value.key?("permissions")
    # Repository default token permissions are not immutable source policy.
    raise "unknown_default_permissions"
  end
  effective_permissions = job.key?("permissions") ?
    permissions(job["permissions"]) : workflow_permissions
  job_environment = scalar_map(job["env"])
  effective_environment = workflow_environment.merge(job_environment)
  job_defaults = defaults(job["defaults"])
  effective_defaults = workflow_defaults.merge(job_defaults)
  effective_defaults_run = {
    "shell" => string_value(effective_defaults["shell"]),
    "working_directory" => string_value(
      effective_defaults["working-directory"]
    )
  }
  steps = job["steps"]
  raise "job_steps" unless steps.is_a?(Array) && !steps.empty?
  normalized_steps = steps.map do |step|
    raise "step_object" unless step.is_a?(Hash)
    uses = step["uses"]
    run = step["run"]
    raise "step_kind" unless uses.is_a?(String) ^ run.is_a?(String)
    step_environment = scalar_map(step["env"])
    effective_step_environment = effective_environment.merge(step_environment)
    run_step = run.is_a?(String)
    normalized = {
      "kind" => (run_step ? "run" : "uses"),
      "name" => string_value(step["name"]),
      "condition" => condition(step["if"]),
      "continue_on_error" => boolean(step["continue-on-error"]),
      "uses" => uses.is_a?(String) ? uses : "",
      "with" => scalar_map(step["with"]),
      "run" => run_step ? run : "",
      "effective_shell" => (
        if run_step
          string_value(
            step["shell"] || effective_defaults["shell"]
          )
        else
          ""
        end
      ),
      "effective_working_directory" => (
        if run_step
          string_value(
            step["working-directory"] ||
            effective_defaults["working-directory"]
          )
        else
          ""
        end
      ),
      "effective_environment" => effective_step_environment
    }
    normalized.merge("secret_refs" => secret_refs(normalized))
  end
  checkouts = normalized_steps.select do |step|
    step["kind"] == "uses" && step["uses"].start_with?("actions/checkout@")
  end
  checkout_persist_credentials = checkouts.any? do |checkout|
    if checkout["with"].key?("persist-credentials")
      v = checkout["with"]["persist-credentials"]
      raise "checkout_credentials" unless [true, false, "true", "false"].include?(v)
      v == true || v == "true"
    else
      true
    end
  end
  runner_environment_asserted = normalized_steps.any? do |step|
    step["kind"] == "run" && step["condition"].empty? &&
      !step["continue_on_error"] &&
      ["", "bash", "sh"].include?(step["effective_shell"]) &&
      [
        'test "$RUNNER_ENVIRONMENT" = github-hosted',
        'test "${RUNNER_ENVIRONMENT:-github-hosted}" = github-hosted'
      ].include?(step["run"].strip)
  end
  environment = job["environment"]
  # An environment object is not the empty environment. The current normalized
  # contract has only a name, so reject richer objects rather than discard them.
  raise "github_environment_object" unless environment.nil? || environment.is_a?(String)
  observed_job = {
    "job_key" => key,
    "job_display_name" => name,
    "condition" => condition(job["if"]),
    "needs" => needs,
    "matrix" => (matrix || {}),
    "continue_on_error" => boolean(job["continue-on-error"]),
    "runs_on" => string_value(job["runs-on"]),
    "effective_defaults_run" => effective_defaults_run,
    "effective_permissions" => effective_permissions,
    "effective_environment" => effective_environment,
    "services" => mapping(job["services"]).transform_values { |v| service(v) },
    "container" => job.key?("container") ? service(job["container"]) : nil,
    "github_environment" => string_value(environment),
    "checkout_persist_credentials" => checkout_persist_credentials,
    "runner_environment_asserted" => runner_environment_asserted,
    "steps" => normalized_steps
  }
  rows << observed_job.merge("secret_refs" => secret_refs(observed_job))
end
name = value["name"] || File.basename(ARGV.fetch(1), File.extname(ARGV.fetch(1)))
raise "workflow_name" unless name.is_a?(String) && !name.strip.empty?
raise "workflow_name" if name.include?("${{") || (selected && !value.key?("name"))
puts JSON.generate({
  "schema" => "normalized_github_workflow_source_v1",
  "workflow_path" => ARGV.fetch(1),
  "workflow_display_name" => name,
  "workflow_name" => name,
  "jobs" => rows.sort_by { |row| row["job_key"] },
  "permissions" => workflow_permissions,
  "environment" => workflow_environment,
  "defaults_run" => {
    "shell" => string_value(workflow_defaults["shell"]),
    "working_directory" => string_value(workflow_defaults["working-directory"])
  },
  "events" => workflow_events
})
"""


def normalize_github_workflow(
    raw: bytes,
    path: str,
    *,
    selected_jobs: list[str] | None = None,
    timeout: float = PREPARE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise CiContractError(f"workflow source exceeds its byte limit: {path}")
    suffix = Path(path).suffix.lower()
    kind = "json" if suffix == ".json" else "yaml"
    result = run_bounded_command(
        ["ruby", "-rjson", "-ryaml", "-e", _ruby_normalizer(), kind, path,
         json.dumps(selected_jobs)],
        stdin=raw,
        timeout=timeout,
        output_limit=MAX_RUBY_OUTPUT_BYTES,
    )
    if result.returncode != 0:
        raise CiContractError("invalid_workflow_source")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CiContractError(f"{path}: normalizer output is invalid") from error
    if not isinstance(value, dict):
        raise CiContractError(f"{path}: normalized workflow is not an object")
    return value


def _required_rows(
    root: Path,
    profile: ProjectProfile,
    target_commit: str,
    repository: str | None = None,
) -> list[dict[str, Any]]:
    if profile.schema_version != 4 or profile.release is None:
        raise CiContractError("source-derived CI requires a schema-4 release profile")
    spec_raw = read_blob_bytes(
        root,
        target_commit,
        profile.release.spec_path,
        label="release spec",
    )
    try:
        spec = ReleaseSpec.from_toml(
            spec_raw.decode("utf-8"),
            profile=profile,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise CiContractError("target release spec is invalid") from error
    if repository is not None and spec.repository != repository:
        raise CiContractError("source_repository_mismatch")
    return [dict(row) for row in spec.required_jobs_v1.jobs]


def _contract_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TOML cannot spell null; accept only the exact empty-container encoding."""
    empty = {"image": "", "environment": {}, "ports": [], "options": ""}
    return [
        {**row, "container": None} if row.get("container") == empty else dict(row)
        for row in rows
    ]


def _observed_sources(
    root: Path, revision: str, rows: list[dict[str, Any]],
) -> list[NormalizedGitHubWorkflowSourceV1]:
    sources = []
    for path in sorted({row["workflow_path"] for row in rows}):
        selected = sorted({row["job_key"] for row in rows if row["workflow_path"] == path})
        raw = read_blob_bytes(
            root, revision, path, label="CI workflow", byte_limit=MAX_WORKFLOW_BYTES,
        )
        observed = normalize_github_workflow(raw, path, selected_jobs=selected)
        # Retain the existing normalizer's workflow_name convenience alias, but
        # the validator receives only the complete source DTO, never policy rows.
        observed = {key: value for key, value in observed.items() if key != "workflow_name"}
        sources.append(NormalizedGitHubWorkflowSourceV1.from_dict(observed))
    return sources


def _profile_at_revision(root: Path, revision: str) -> ProjectProfile:
    raw = read_blob_bytes(
        root,
        revision,
        ".kent/workflow-profile.toml",
        label="project profile",
    )
    try:
        return ProjectProfile.from_toml(
            root,
            raw.decode("utf-8"),
            source=f"{revision}:.kent/workflow-profile.toml",
            check_files=False,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise CiContractError("project profile at the selected revision is invalid") from error


def _validate_job_sources(
    root: Path,
    target_commit: str,
    head_commit: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        if not rows:
            raise CiContractError("required_jobs_empty")
        contracts = {"schema": "required_jobs_v1", "jobs": _contract_rows(rows)}
        p_bindings = validate_required_job_sources(
            _observed_sources(root, target_commit, rows), contracts,
        )
        h_bindings = validate_required_job_sources(
            _observed_sources(root, head_commit, rows), contracts,
        )
        def checks(bindings):
            return sorted(
                [
                    {"workflow_name": item.workflow["workflow_display_name"],
                     "check_name": item.job["job_display_name"], "allow_skipped": False}
                    for item in bindings
                ],
                key=lambda item: (item["workflow_name"], item["check_name"]),
            )
        result = checks(p_bindings)
        if result != checks(h_bindings):
            raise CiContractError("required_job_identity_drift")
    except (ReleaseSpecError, KeyError, TypeError) as error:
        raise CiContractError("required_job_source_invalid") from error
    identities = {
        (item["workflow_name"], item["check_name"]) for item in result
    }
    if len(identities) != len(result):
        raise CiContractError("required CI jobs contain duplicate identities")
    return sorted(result, key=lambda item: (item["workflow_name"], item["check_name"]))


def _source_policy_projection(
    root: Path,
    target_commit: str,
    profile: ProjectProfile,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    bindings = validate_required_job_sources(
        _observed_sources(root, target_commit, rows),
        {"schema": "required_jobs_v1", "jobs": _contract_rows(rows)},
    )
    # Effective required semantics only: unrelated release fields, unselected
    # jobs, comments, and redundant workflow defaults do not start a new cycle.
    return {"required": sorted(
        [{"workflow_path": b.workflow_path,
          "workflow_name": b.workflow["workflow_display_name"],
          "event": b.event, "job": b.job, "policy": b.policy,
          "step_overlays": list(b.step_overlays)} for b in bindings],
        key=lambda row: (row["workflow_path"], row["job"]["job_key"]),
    )}


def derive_expected_ci_checks(
    root: Path,
    *,
    repository: str,
    target_commit: str,
    head_commit: str,
    runtime_source_envelope_digest: str,
) -> dict[str, Any]:
    if (
        not isinstance(runtime_source_envelope_digest, str)
        or len(runtime_source_envelope_digest) != 64
        or set(runtime_source_envelope_digest) - SHA1_HEX
    ):
        raise CiContractError("runtime source envelope digest is invalid")
    profile = _profile_at_revision(root, target_commit)
    rows = _required_rows(root, profile, target_commit)
    checks = _validate_job_sources(root, target_commit, head_commit, rows)
    return {
        "schema": "github-ci-expected-checks-v1",
        "repository": repository,
        "project_commit": head_commit,
        "runtime_source_envelope_digest": runtime_source_envelope_digest,
        "checks": checks,
    }


def prepare_ci_payload(
    payload: Mapping[str, Any],
    *,
    gh_bin: str | None = None,
) -> dict[str, Any]:
    validate_preparation_identity(payload)
    for key in (
        "ci_contract", "ci_report", "pr_feedback_cursor",
    ):
        if key in payload and not isinstance(payload[key], str):
            raise CiContractError("input_invalid")
    return _prepare_ci_payload(payload, gh_bin=gh_bin)


def _dynamic_pull_number(pr_url: str) -> int:
    parts = [part for part in urlparse(pr_url).path.split("/") if part]
    if len(parts) != 4:
        raise CiContractError("pr_url must identify one GitHub pull request")
    try:
        number = int(parts[3])
    except ValueError as error:
        raise CiContractError("pr_url must identify one GitHub pull request") from error
    if number < 1 or number > 2147483647:
        raise CiContractError("pr_url must identify one GitHub pull request")
    return number


def _dynamic_contract(
    *,
    repository: str,
    pull_number: int,
    head_oid: str,
    base_oid: str,
) -> dict[str, Any]:
    return validate_ci_contract({
        "schema": "github-ci-contract-v1",
        "repository": repository,
        "pull_number": pull_number,
        "head_oid": head_oid,
        "base_oid": base_oid,
        "require_ci": True,
    })


def _validate_dynamic_prepare_packets(
    payload: Mapping[str, Any],
    *,
    repository: str,
    pull_number: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    contract: dict[str, Any] | None = None
    raw_contract = payload.get("ci_contract", "")
    if raw_contract:
        try:
            contract = validate_ci_contract(parse_preparation_json(raw_contract))
        except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
            raise CiContractError("previous CI contract is invalid") from error
        if (
            contract["repository"] != repository
            or contract["pull_number"] != pull_number
        ):
            raise CiContractError("previous CI contract identity is invalid")
    report: dict[str, Any] | None = None
    raw_report = payload.get("ci_report", "")
    if raw_report:
        try:
            report = validate_dynamic_ci_report(parse_preparation_json(raw_report))
        except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
            raise CiContractError("previous CI report is invalid") from error
        report_contract = report["contract"]
        if (
            report_contract["repository"] != repository
            or report_contract["pull_number"] != pull_number
        ):
            raise CiContractError("previous CI report identity is invalid")
    cursor = payload.get("pr_feedback_cursor", "uninitialized")
    if cursor != "uninitialized":
        try:
            validate_dynamic_pr_feedback_cursor(parse_preparation_json(cursor))
        except (CiContractError, RuntimeContractError, TypeError, ValueError) as error:
            raise CiContractError("previous PR feedback cursor is invalid") from error
    return contract, report, cursor


def _dynamic_prepare_result(
    *,
    transition: str,
    workspace: Path,
    payload: Mapping[str, Any],
    contract: Mapping[str, Any],
    cursor: str,
    merge_report: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "transition": transition,
        "workspace_path": str(workspace),
        "pr_url": str(payload["pr_url"]),
        "branch_name": str(payload["branch_name"]),
        "merge_strategy": str(payload["merge_strategy"]),
        "pr_head_oid": contract["head_oid"],
        "pr_base_oid": contract["base_oid"],
        "task_short_id": str(payload["task_short_id"]),
        "ci_contract": canonical_bytes(contract).decode("utf-8"),
        "pr_feedback_cursor": cursor,
    }
    if merge_report is not None:
        result["merge_report"] = merge_report
    return result


def _prepare_ci_payload(
    payload: Mapping[str, Any], *, gh_bin: str | None = None,
) -> dict[str, Any]:
    workspace = Path(payload["workspace_path"]).resolve()
    pr_url = payload["pr_url"]
    repository = _repository_from_url(pr_url)
    pull_number = _dynamic_pull_number(pr_url)
    first = read_pull_request(workspace, pr_url, gh_bin=gh_bin)
    _validate_pr_metadata(first, payload)
    head = first["headRefOid"]
    target = first["baseRefOid"]
    contract = _dynamic_contract(
        repository=repository,
        pull_number=pull_number,
        head_oid=head,
        base_oid=target,
    )
    prior_contract, previous_report, cursor = _validate_dynamic_prepare_packets(
        payload,
        repository=repository,
        pull_number=pull_number,
    )
    if str(first.get("state") or "").upper() == "MERGED":
        if previous_report is not None:
            archive_ci_report(workspace, payload["task_short_id"], previous_report)
        return _dynamic_prepare_result(
            transition="ci_prepare_pr_merged",
            workspace=workspace,
            payload=payload,
            contract=contract,
            cursor=cursor,
            merge_report=json.dumps(first, sort_keys=True),
        )
    second = read_pull_request(workspace, pr_url, gh_bin=gh_bin)
    _validate_pr_metadata(second, payload)
    if second["state"] == "MERGED":
        if previous_report is not None:
            archive_ci_report(workspace, payload["task_short_id"], previous_report)
        contract = _dynamic_contract(
            repository=repository,
            pull_number=pull_number,
            head_oid=second["headRefOid"],
            base_oid=second["baseRefOid"],
        )
        return _dynamic_prepare_result(
            transition="ci_prepare_pr_merged",
            workspace=workspace,
            payload=payload,
            contract=contract,
            cursor=cursor,
            merge_report=json.dumps(second, sort_keys=True),
        )
    if (
        second["headRefOid"] != head
        or str(second.get("baseRefOid") or "") != target
        or second.get("baseRefName") != first.get("baseRefName")
    ):
        raise CiContractError("pull request identity changed during CI preparation")
    if prior_contract is not None and (
        prior_contract["head_oid"] != head
        or prior_contract["base_oid"] != target
    ):
        same_cycle = False
    else:
        same_cycle = (
            previous_report is not None
            and previous_report["contract"] == contract
        )
    if previous_report is not None:
        archive_ci_report(workspace, payload["task_short_id"], previous_report)
    result = _dynamic_prepare_result(
        transition=(
            "ci_prepare_ready_retry" if same_cycle else "ci_prepare_ready_initial"
        ),
        workspace=workspace,
        payload=payload,
        contract=contract,
        cursor=cursor,
    )
    if same_cycle:
        result["ci_report"] = str(payload["ci_report"])
    return result


__all__ = [
    "BoundedCommandError",
    "CiContractError",
    "derive_expected_ci_checks",
    "normalize_github_workflow",
    "prepare_ci_payload",
    "read_pull_request",
    "run_bounded_command",
]
