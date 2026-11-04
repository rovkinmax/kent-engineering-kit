"""Fresh ordinary-R2 registration; no source admission or global installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid

from . import operations as op
from .project_memory import ProjectMemory, MissingMemory, MemoryError as ProjectMemoryError

SCHEMA = "r2-project-install-plan-v1"
LIMIT = op.SOURCE_RECEIPT_BYTES_LIMIT
KIT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise op.PlanValidationError(message)


def path(value):
    return op._source_canonical_path(str(value), "installation path")


def read_private(filename):
    return op._read_source_record(
        filename, label="installation record", maximum=LIMIT, private=True,
    )[1]


@contextmanager
def private_directory(filename, *, create=False):
    """Open through no-follow dirfds; never repair foreign permissions."""
    filename = path(filename)
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY)]
    chain = []
    try:
        for index, name in enumerate(filename.parts[1:]):
            parent = descriptors[-1]
            if create and index == len(filename.parts) - 2:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            chain.append((parent, name, child))
        info = os.fstat(descriptors[-1])
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "installation directory must be owned and private 0700")
        yield descriptors[-1]
        for parent, name, child in chain:
            linked, opened = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(child)
            require((linked.st_dev, linked.st_ino) == (opened.st_dev, opened.st_ino),
                    "installation directory changed")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish(filename, raw):
    """Fsync complete bytes before exclusive link publication; never replace."""
    with private_directory(filename.parent) as directory:
        if os.path.lexists(filename):
            require(read_private(filename) == raw, "existing installation record conflicts")
            return False
        temporary = ".publish-" + uuid.uuid4().hex
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        try:
            remaining = memoryview(raw)
            while remaining:
                written = os.write(descriptor, remaining)
                require(written > 0, "installation write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # A crash before link leaves only a private staging file, never partial JSON.
        os.link(temporary, filename.name, src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)
        os.fsync(directory)
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    require(read_private(filename) == raw, "installation readback differs")
    return True


def environment():
    home = path(Path.home())
    kent_root = home / ".kent"
    require(path(os.environ.get("KENT_PERSISTENCE_ROOT", kent_root)) == kent_root,
            "custom persistence root is not supported by the installed binding reader")
    executable = shutil.which("kent")
    require(executable is not None, "Kent executable unavailable")
    kent = op._kent_path(str(Path(executable).resolve()))
    return kent_root, kent


def native_snapshot(kent, primary, project_id, workflow_id):
    workspace = op._source_native_identity(
        project_id=project_id, project_root=primary, primary_root=primary,
        kent_path=kent, source_only=True,
    )
    pages = op._kent_pages(kent, ["task", "list", "--project", project_id], cwd=primary)
    # Native task/list is project scoped. A terminal page must have explicit
    # project scope and rows; empty output without that evidence is not inventory.
    for page in pages:
        require(isinstance(page.get("scope"), dict) and page["scope"].get("project_id") == project_id,
                "native Task inventory project differs")
    require(not op._page_rows(pages, ("items", "tasks")),
            "fresh installation requires zero native Tasks; use separately approved adoption")
    workflows = op._page_rows(
        op._kent_pages(kent, ["workflow", "list", "--project", project_id], cwd=primary),
        ("items", "workflows"),
    )
    links, default, version = op._workflow_link_state(workflows, project_id, workflow_id)
    require(len(links) == 1 and default is None, "explicit linked non-default Workflow required")
    validation = op._kent_json(kent, ["workflow", "validate", workflow_id, "--json"], cwd=primary)
    require(validation.get("valid") is True, "native Workflow validation failed")
    return {"primary_workspace_id": workspace, "workflow_version": version, "task_ids": []}


def storage_bindings(kent_root, kit):
    """Qualify installed storage; observe CI without admitting or changing it."""
    ci = kent_root / "bin/kent-prepare-github-ci"
    rows = [{"link": str(kent_root / "bin/kent-project-memory"),
             "target": str(kit / ".kent/scripts/workflow-project-memory")}]
    for row in rows:
        link, target = path(row["link"]), path(row["target"])
        parent = op._source_canonical_root(str(link.parent), "storage helper directory")
        parent_info, info = parent.stat(), link.lstat()
        require(parent_info.st_uid == os.getuid() and not parent_info.st_mode & 0o7022
                and info.st_uid == os.getuid() and info.st_nlink == 1
                and stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o7000
                and link.resolve(strict=True) == target,
                "installed storage alias differs; project installer never creates aliases")
    ci_target = str(ci.resolve(strict=True)) if ci.exists() else None
    observation = {"link": str(ci), "target": ci_target,
                   "matches_publication": ci_target == str(kit / "scripts/prepare-github-ci")}
    return ci, rows, observation


def qualify_storage(receipt, primary, kit):
    """Require the actual published adapter/core pairs, not CI/source admission."""
    op.validate_runtime_v3_receipt(receipt, primary, kit)
    files = {}
    for name in (".kent/scripts/workflow-project-memory",
                 "templates/project/workflow-project-memory",
                 ".kent/scripts/workflow_project_memory.py", "workflowkit/project_memory.py"):
        rows = [item for item in receipt["files"]
                if item["root"] == str(kit) and item["relative_path"] == name]
        require(len(rows) == 1, "storage publication closure incomplete")
        _, raw, mode = op._read_source_record(
            kit / name, label="published storage source",
            maximum=op.SOURCE_FILE_BYTES_LIMIT, private=False,
        )
        item = rows[0]
        require(op.sha256_bytes(raw) == item["sha256"] and len(raw) == item["byte_size"]
                and mode == item["mode"] and mode & 0o400,
                "installed storage differs from its published receipt")
        if name.endswith("workflow-project-memory"):
            require(mode & 0o100, "published storage adapter is not executable")
        files[name] = raw
    require(files[".kent/scripts/workflow-project-memory"]
            == files["templates/project/workflow-project-memory"]
            and files[".kent/scripts/workflow_project_memory.py"] == files["workflowkit/project_memory.py"],
            "published storage adapter/core copies differ")


def empty_store(memory):
    store = ProjectMemory(Path(memory["root"]), memory["project_key"], memory["project_uid"])
    if not os.path.lexists(store.path):
        # Qualify the existing shared root without creating it or touching peers.
        if os.path.lexists(store.root):
            with store._directory(include_project=False):
                pass
        else:
            op._source_canonical_root(str(store.root.parent), "R2 parent directory")
        return
    store._project()
    with store._directory() as directory:
        require(set(os.listdir(directory)) <= {"project.json", ".lock", "knowledge", "tasks"},
                "fresh project namespace contains unknown state")
    for name in ("knowledge", "tasks"):
        try:
            with store._directory(name) as directory:
                require(not os.listdir(directory), "fresh project namespace is not empty")
        except MissingMemory:
            raise op.PlanValidationError("partial project namespace requires operator recovery") from None


def build_plan(*, project, workflow, source_ref, kit_ref):
    kent_root, kent = environment()
    primary = op._source_canonical_root(str(project), "installation primary checkout")
    require(op._git(primary, "rev-parse", "--show-toplevel") == str(primary),
            "--project must be a checkout root")
    # Reject managed Git worktrees rather than resolving them to another root.
    git_dir = Path(op._git(primary, "rev-parse", "--absolute-git-dir"))
    common = Path(op._git(primary, "rev-parse", "--git-common-dir"))
    common = (primary / common).resolve() if not common.is_absolute() else common.resolve()
    require(git_dir == common, "installation from a managed worktree is not supported")
    project_ids = [row["id"] for row in op._project_rows(kent, primary)
                   if row["path"] == str(primary)]
    require(len(project_ids) == 1, "explicit primary native project identity unavailable")
    project_id = op._native_project_id(project_ids[0], "installation project")
    workflow = op._uuid(workflow, "installation Workflow")
    native = native_snapshot(kent, primary, project_id, workflow)
    profile_path = primary / ".kent/workflow-profile.toml"
    profile_raw = op._read_source_record(
        profile_path, label="primary profile", maximum=LIMIT, private=False,
    )[1]
    profile = op.tomllib.loads(profile_raw.decode("utf-8"))
    memory = profile.get("project_memory")
    require(isinstance(memory, dict) and set(memory) == {
        "root", "project_key", "project_uid", "mode",
    } and memory["mode"] == "r2", "explicit complete R2 profile required")
    require(profile.get("policies", {}).get("delivery_reliability") == "v1",
            "this first installer supports the existing delivery-reliability-v1 publication contract")
    root = path(memory["root"])
    require(root != primary and not root.is_relative_to(primary) and root != KIT
            and not root.is_relative_to(KIT), "R2 root must be external to project and Kit")
    empty_store(memory)
    ci_helper, helpers, ci_observation = storage_bindings(kent_root, KIT)
    receipt = op.prepare_initial_runtime_publication_v3(
        project_id=project_id, receipt_workflow_id=workflow, primary_root=str(primary),
        primary_workspace_id=native["primary_workspace_id"],
        source_commit=op._git(primary, "rev-parse", "--verify", source_ref),
        source_ref=source_ref, kit_root=str(KIT),
        kit_commit=op._git(KIT, "rev-parse", "--verify", kit_ref), kit_ref=kit_ref,
        ci_helper=str(ci_helper), helper_bindings=helpers,
        project_memory={name: memory[name] for name in ("root", "project_key", "project_uid")} |
                       {"legacy_task_ids": []},
    )
    qualify_storage(receipt, primary, KIT)
    receipt_raw = op.canonical_bytes(receipt)
    receipt_path = kent_root / "project-memory-installations" / project_id / "receipt.json"
    binding_path = kent_root / "project-memory-bindings" / (project_id + ".json")
    binding = {
        "schema": "r2-installation-binding-v2", "project_id": project_id,
        "primary_root": str(primary), "primary_workspace_id": native["primary_workspace_id"],
        "profile_sha256": op.sha256_bytes(profile_raw), "receipt_path": str(receipt_path),
        "receipt_sha256": op.sha256_bytes(receipt_raw), "kit_root": str(KIT),
        "source_ref": source_ref, "kit_ref": kit_ref,
        "ci_helper": str(ci_helper), "helper_bindings": helpers,
    }
    for filename, raw in ((receipt_path, receipt_raw), (binding_path, op.canonical_bytes(binding))):
        if os.path.lexists(filename):
            require(read_private(filename) == raw, "existing installation conflicts")
    require(native_snapshot(kent, primary, project_id, workflow) == native,
            "native inventory changed during preview")
    return {
        "schema": SCHEMA, "project": str(primary), "workflow": workflow,
        "source_ref": source_ref, "kit_ref": kit_ref,
        "kent_path": str(kent), "kent_sha256": op.sha256_bytes(kent.read_bytes()),
        "native": native, "receipt": receipt, "binding": binding,
        "binding_path": str(binding_path),
        "ci_helper_observation": ci_observation, "source_admitted": False, "qualified": False,
    }


def readback(plan):
    """Use the same installation reader as ordinary native storage."""
    target = KIT / ".kent/scripts/workflow-project-memory"
    spec = importlib.util.spec_from_loader(
        "qualified_project_install_reader",
        SourceFileLoader("qualified_project_install_reader", str(target)),
    )
    require(spec is not None and spec.loader is not None, "qualified installation reader unavailable")
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    result = reader.installation_records(
        plan["receipt"]["project_id"], Path(plan["project"]),
        plan["native"]["primary_workspace_id"],
    )
    require(result["mode"] == "r2" and result["binding"] == plan["binding"]
            and result["receipt"] == plan["receipt"], "ordinary installation reader differs")
    core = reader.load_core(Path(plan["binding"]["kit_root"]), plan["receipt"])
    memory = plan["receipt"]["project_memory"]
    core.ProjectMemory(Path(memory["root"]), memory["project_key"], memory["project_uid"])._project()


def apply(plan, expected_sha256, confirmation):
    require(op.sha256_bytes(op.canonical_bytes(plan)) == expected_sha256, "installation plan digest differs")
    require(confirmation == plan.get("receipt", {}).get("project_id"), "confirm exact native project ID")
    require(plan.get("schema") == SCHEMA, "unsupported installation plan")

    def refresh():
        actual = build_plan(**{key: plan[key] for key in ("project", "workflow", "source_ref", "kit_ref")})
        require(actual == plan, "installation preconditions changed; prepare a new reviewed preview")

    refresh()  # No effects before revalidating the entire closed plan.
    binding_path = Path(plan["binding_path"])
    receipt_path = Path(plan["binding"]["receipt_path"])
    if os.path.lexists(binding_path):
        readback(plan)
        return registration_result("already-installed")
    # Existing global installation and aliases are prerequisites, not effects.
    for directory in (receipt_path.parent.parent, receipt_path.parent, binding_path.parent):
        with private_directory(directory, create=True):
            pass
    with private_directory(receipt_path.parent) as directory:
        flags = os.O_RDWR | os.O_NOFOLLOW
        created = False
        try:
            lock = os.open(".lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            created = True
        except FileExistsError:
            lock = os.open(".lock", flags, dir_fd=directory)
        try:
            if created:
                os.fsync(directory)
            info = os.fstat(lock)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                    "installation lock is unsafe")
            fcntl.flock(lock, fcntl.LOCK_EX)
            linked = os.stat(".lock", dir_fd=directory, follow_symlinks=False)
            require((linked.st_dev, linked.st_ino) == (info.st_dev, info.st_ino),
                    "installation lock changed")
            refresh()
            if os.path.lexists(binding_path):
                readback(plan)
                return registration_result("already-installed")
            memory = plan["receipt"]["project_memory"]
            ProjectMemory.initialize(Path(memory["root"]), memory["project_key"], memory["project_uid"])
            publish(receipt_path, op.canonical_bytes(plan["receipt"]))
            refresh()
            try:
                publish(binding_path, op.canonical_bytes(plan["binding"]))
                readback(plan)
                refresh()
            except Exception as error:
                raise op.EffectBlocked(
                    "binding published but installation is UNQUALIFIED; preserve records for recovery"
                ) from error
        finally:
            os.close(lock)
    return registration_result("installed")


def registration_result(status):
    return {"status": status, "storage_registered": True, "source_admitted": False, "qualified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    preview = commands.add_parser("preview")
    for name in ("project", "workflow", "source-ref", "kit-ref"):
        preview.add_argument("--" + name, required=True)
    commit = commands.add_parser("apply")
    commit.add_argument("--plan", required=True, type=Path)
    commit.add_argument("--expect-plan-sha256", required=True)
    commit.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        if args.mode == "preview":
            result = build_plan(project=args.project, workflow=args.workflow,
                                source_ref=args.source_ref, kit_ref=args.kit_ref)
        else:
            raw = read_private(path(args.plan))
            require(op.sha256_bytes(raw) == args.expect_plan_sha256, "installation plan bytes differ")
            plan = op._source_json(raw, "installation plan")
            require(op.canonical_bytes(plan) == raw, "installation plan must use canonical JSON")
            result = apply(plan, args.expect_plan_sha256, args.confirm)
        sys.stdout.buffer.write(op.canonical_bytes(result))
        return 0
    except (op.OperationError, OSError, ValueError, ProjectMemoryError) as error:
        print(f"install project: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
