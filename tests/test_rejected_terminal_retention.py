from __future__ import annotations

import copy
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / ".kent/recovery/KEN-21/entry-prompts.json"
PATCH_PATH = ROOT / ".kent/recovery/KEN-21/janitor-retention.patch"
FROZEN_WORKTREE = Path(
    "/Users/rovkinmax/.kent/worktrees/kent-engineering-kit/KEN-21"
)
NATIVE_GRAPH_PATH = ROOT / "build/kent-workflow/KEN-22/native-graph-before.json"

TASK_ID = "task-942deecd-ed03-450c-8183-9c3edfa06b8e"
TASK_SHORT_ID = "KEN-21"
PROJECT_ID = "project-135413f9-2454-4d04-969e-3831b1527f18"
WORKTREE_ID = "5dadde9d-2b63-4c8d-b2f4-dd1f7a9741f5"
JANITOR_NODE_ID = "407ed282-e435-4ab1-a644-b511b749c0d7"
PENDING_APPROVAL_SNAPSHOT_REF = "dc8aa6c4-bdfd-4730-b7ad-1cdf423817af"
FROZEN_HEAD = "c6d429ae8761c13f2d1f16624e7aeb6c687ae855"
JANITOR_PATH = ".kent/scripts/workflow-task-janitor"
FROZEN_JANITOR_SHA256 = (
    "fe09b51b60b5e6dfd0ef188ed0c89533a47581dbcc5fdc1d8b0d0647bff24ebe"
)
PATCH_FILE_SHA256 = (
    "61133a27e2f8df65567f3bfbe358532f4f008ef95123bfa219d8ab7013728799"
)
PATCH_DIFF_SHA256 = (
    "447622409b0218ab4756abcfa8c22f61701bcddf44632ec02ffbc423c2c2ed75"
)
PATCHED_JANITOR_SHA256 = (
    "aa150af01ab8d980073fec0fcbdf98817569553289e24bc8aea34a46fb78020f"
)
NATIVE_GRAPH_SHA256 = (
    "fd8cfb44aaeb1e48a4b82de98c22112a60cfb0f3e571d3d3ddecaff35e0a45c9"
)
PROMPT_ARTIFACT_SHA256 = (
    "23b63231c88d4e90cc7f1b91ac5ebcbaef5e4dc31c09e3b66805459804c48372"
)
PROMPT_HASHES = {
    "prepare_pr_waiting_pr": (
        "d4c24a9423368b3dc88d180e119bb2c1aef711f7e223f83af32ebfb3354e56ef"
    ),
    "waiting_pr_cleanup": (
        "d8413766d2e962511a8642d92350b3a89a1e4867526ed73fd2cefa7032f6c7f4"
    ),
    "waiting_pr_needs_user_action": (
        "d4c24a9423368b3dc88d180e119bb2c1aef711f7e223f83af32ebfb3354e56ef"
    ),
    "task_janitor_blocked": (
        "63b91ddd7d2b5abcdfac96c4338935f692b956d093d31ec9d39797f76e09db17"
    ),
    "cleanup_needs_user_action": (
        "ae73912332eb763a1a3e32896e5cc0002ddf31d4a30a521fe5ac504209af5646"
    ),
}
PROMPT_KEYS = list(PROMPT_HASHES)
PROMPT_ENTRY_FIELDS = {
    "transition_key",
    "original_prompt_sha256",
    "original_prompt",
    "target_prompt",
    "replacement_prompt",
}
PROMPT_GUARD = (
    '{{if and (eq .TaskId "task-942deecd-ed03-450c-8183-9c3edfa06b8e") '
    '(eq .TaskShortId "KEN-21")}}'
)

# These incident qualification cases require private snapshots and Git history.
# They are explicit opt-in checks, not part of checkout-local source validation.
LOCAL_QUALIFICATION_TESTS = {
    "test_native_snapshot_exposes_only_the_supported_recovery_route",
    "test_disposable_patched_main_retains_state_and_repeated_observation",
    "test_invalid_identity_consent_modes_and_readback_fail_closed",
    "test_original_drift_links_modes_missing_and_extra_state_block",
    "test_janitor_patch_applies_to_exact_frozen_preimage_and_compiles",
}


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(RejectedTerminalRetentionTests):
        if (
            name not in LOCAL_QUALIFICATION_TESTS
            or os.environ.get("KEN22_RUN_LOCAL_QUALIFICATION") == "1"
        ):
            suite.addTest(RejectedTerminalRetentionTests(name))
    return suite


GO_TEMPLATE_CHECK = r'''package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"text/template"
)

type Entry struct {
	Key         string `json:"transition_key"`
	Original    string `json:"original_prompt"`
	Target      string `json:"target_prompt"`
	Replacement string `json:"replacement_prompt"`
}

type Packet struct {
	TaskID      string  `json:"task_id"`
	TaskShortID string  `json:"task_short_id"`
	Entries     []Entry `json:"entries"`
}

type PromptData struct {
	TaskId      string
	TaskShortId string
	TaskTitle   string
	Params      map[string]any
}

func render(name, source string, data PromptData) (string, error) {
	parsed, err := template.New(name).Parse(source)
	if err != nil {
		return "", err
	}
	var output bytes.Buffer
	if err := parsed.Execute(&output, data); err != nil {
		return "", err
	}
	return output.String(), nil
}

func main() {
	var packet Packet
	if err := json.NewDecoder(os.Stdin).Decode(&packet); err != nil {
		panic(err)
	}
	params := map[string]any{
		"pr_url": "https://github.com/rovkinmax/kent-engineering-kit/pull/36",
		"branch_name": "KEN-21",
		"merge_strategy": "rebase",
		"workspace_path": "/Users/rovkinmax/.kent/worktrees/kent-engineering-kit/KEN-21",
		"merge_report": "independent merge readback",
		"cleanup_report": "prior cleanup report",
		"blocker_reason": "exact test blocker",
	}
	for _, entry := range packet.Entries {
		targetData := PromptData{
			TaskId: packet.TaskID, TaskShortId: packet.TaskShortID,
			TaskTitle: "Retain rejected terminal evidence", Params: params,
		}
		target, err := render(entry.Key, entry.Replacement, targetData)
		if err != nil {
			panic(fmt.Errorf("%s target: %w", entry.Key, err))
		}
		if target != entry.Target {
			panic(fmt.Errorf("%s did not select its task-only target branch", entry.Key))
		}
		for _, identity := range []struct{ taskID, shortID string }{
			{"task-other-00000000-0000-0000-0000-000000000000", "KEN-21"},
			{packet.TaskID, "KEN-20"},
			{"task-other-00000000-0000-0000-0000-000000000000", "KEN-20"},
		} {
			data := PromptData{
				TaskId: identity.taskID, TaskShortId: identity.shortID,
				TaskTitle: "Retain rejected terminal evidence", Params: params,
			}
			expected, err := render(entry.Key+"-original", entry.Original, data)
			if err != nil {
				panic(fmt.Errorf("%s original: %w", entry.Key, err))
			}
			actual, err := render(entry.Key+"-replacement", entry.Replacement, data)
			if err != nil {
				panic(fmt.Errorf("%s fallback: %w", entry.Key, err))
			}
			if actual != expected {
				panic(fmt.Errorf("%s changed non-target rendered prompt bytes", entry.Key))
			}
		}
	}
}
'''


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


class RejectedTerminalRetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prompt_packet = json.loads(PROMPT_PATH.read_text(encoding="utf-8"))

    def _git(self, root: Path, *arguments: str) -> str:
        result = subprocess.run(
            [shutil.which("git") or "git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            env=_git_environment(),
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def _make_retention_fixture(self, directory: Path) -> dict[str, object]:
        if not FROZEN_WORKTREE.is_dir():
            self.fail("explicit local qualification requires the frozen KEN-21 worktree")
        directory = directory.resolve()
        git_env = _git_environment()
        source_head = self._git(FROZEN_WORKTREE, "rev-parse", "HEAD").strip()
        self.assertEqual(source_head, FROZEN_HEAD)
        source_status = self._git(
            FROZEN_WORKTREE, "status", "--porcelain=v1", "--untracked-files=all",
        )
        self.assertEqual(source_status, "")

        git = shutil.which("git")
        self.assertIsNotNone(git)
        home = directory / "home"
        primary = home / "primary"
        workspace = (
            home / ".kent" / "worktrees" / "kent-engineering-kit" / "KEN-21"
        )
        primary.parent.mkdir(parents=True)
        subprocess.run(
            [
                git,
                "clone",
                "--quiet",
                "--shared",
                "--branch",
                TASK_SHORT_ID,
                "--no-checkout",
                str(FROZEN_WORKTREE),
                str(primary),
            ],
            check=True,
            capture_output=True,
            env=git_env,
            timeout=120,
        )
        subprocess.run(
            [git, "-C", str(primary), "switch", "--detach", FROZEN_HEAD],
            check=True,
            capture_output=True,
            env=git_env,
            timeout=120,
        )
        workspace.parent.mkdir(parents=True)
        subprocess.run(
            [
                git,
                "-C",
                str(primary),
                "worktree",
                "add",
                "--quiet",
                str(workspace),
                TASK_SHORT_ID,
            ],
            check=True,
            capture_output=True,
            env=git_env,
            timeout=120,
        )
        self.assertEqual(self._git(workspace, "rev-parse", "HEAD").strip(), FROZEN_HEAD)
        self.assertEqual(
            self._git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD").strip(),
            TASK_SHORT_ID,
        )

        source_build = FROZEN_WORKTREE / "build/kent-workflow/KEN-21"
        self.assertTrue(source_build.is_dir())
        self.assertFalse(
            any(path.is_symlink() for path in source_build.rglob("*")),
            "the frozen fixture unexpectedly contains symlinks",
        )
        (workspace / "build/kent-workflow").mkdir(parents=True)
        shutil.copytree(
            source_build,
            workspace / "build/kent-workflow/KEN-21",
            copy_function=shutil.copy2,
        )

        task_name_hash = hashlib.sha256(TASK_SHORT_ID.encode("utf-8")).hexdigest()
        lock_name = f".evidence-lock-{task_name_hash}"
        source_runtime = FROZEN_WORKTREE / ".kent/runtime"
        self.assertEqual(
            {item.name for item in source_runtime.iterdir()},
            {lock_name, TASK_SHORT_ID},
        )
        runtime_root = workspace / ".kent/runtime"
        task_runtime = runtime_root / TASK_SHORT_ID
        task_runtime.mkdir(parents=True)
        shutil.copy2(
            source_runtime / lock_name,
            runtime_root / lock_name,
        )
        shutil.copy2(
            source_runtime / TASK_SHORT_ID / "evidence-ledger.jsonl",
            task_runtime / "evidence-ledger.jsonl",
        )

        script = workspace / JANITOR_PATH
        subprocess.run(
            [git, "-C", str(workspace), "apply", "--check", str(PATCH_PATH)],
            check=True,
            capture_output=True,
            env=git_env,
        )
        subprocess.run(
            [git, "-C", str(workspace), "apply", str(PATCH_PATH)],
            check=True,
            capture_output=True,
            env=git_env,
        )
        patched = script.read_bytes()
        self.assertEqual(
            hashlib.sha256(patched).hexdigest(),
            PATCHED_JANITOR_SHA256,
        )
        original_root_assignment = (
            f'KEN21_RETENTION_ROOT = {json.dumps(str(FROZEN_WORKTREE))}\n'
        ).encode("utf-8")
        fixture_root_assignment = (
            f"KEN21_RETENTION_ROOT = {json.dumps(str(workspace))}\n"
        ).encode("utf-8")
        self.assertEqual(patched.count(original_root_assignment), 1)
        script.write_bytes(patched.replace(
            original_root_assignment,
            fixture_root_assignment,
            1,
        ))
        script.chmod(0o700)
        self.assertEqual(
            self._git(workspace, "status", "--porcelain=v1", "--untracked-files=all"),
            f" M {JANITOR_PATH}\n",
        )
        self.assertEqual(
            self._git(workspace, "diff", "--name-only", "HEAD", "--").splitlines(),
            [JANITOR_PATH],
        )
        self.assertEqual(
            self._git(workspace, "diff", "--cached", "--name-only", "HEAD", "--"),
            "",
        )
        self.assertEqual(
            self._git(workspace, "ls-files", "--others", "--exclude-standard"),
            "",
        )

        prior_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            loader = importlib.machinery.SourceFileLoader(
                "ken21_retention_fixture_janitor",
                str(script),
            )
            spec = importlib.util.spec_from_loader(
                "ken21_retention_fixture_janitor",
                loader,
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = prior_bytecode

        support = directory / "support"
        support.mkdir()
        wrapper = support / "kent-worktree"
        wrapper.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
mode = os.environ.get("KEN21_WRAPPER_MODE", "deny-write")
with open(os.environ["KEN21_WRAPPER_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if not args:
    sys.stderr.write("missing wrapper action\\n")
    raise SystemExit(77)
if args[0] == "list":
    if mode in {"deny-list", "interrupt-list"}:
        sys.stderr.write("native identity readback denied\\n")
        raise SystemExit(78 if mode == "interrupt-list" else 77)
    primary = os.environ["KEN21_PRIMARY"]
    root = os.environ["KEN21_TASK_ROOT"]
    worktree_id = os.environ.get(
        "KEN21_WRAPPER_WORKTREE_ID",
        "5dadde9d-2b63-4c8d-b2f4-dd1f7a9741f5",
    )
    print(json.dumps({"worktrees": [
        {"topology": {"mainWorkspace": {"git": {
            "canonicalRoot": primary, "isMainWorktree": True,
        }}}},
        {"topology": {"registered": {
            "git": {"canonicalRoot": root},
            "kent": {
                "canonicalRoot": root,
                "worktreeId": worktree_id,
                "managed": True,
            },
        }}},
    ]}))
elif args[0] == "status":
    if mode in {"deny-status", "interrupt-status"}:
        sys.stderr.write("native owner readback denied\\n")
        raise SystemExit(78 if mode == "interrupt-status" else 77)
    effective = os.environ["KEN21_PRIMARY"]
    if mode == "owner-active":
        effective = os.environ["KEN21_TASK_ROOT"]
    print(json.dumps({"target": {"effectiveWorkdir": effective}, "worktree": None}))
else:
    sys.stderr.write("native mutation denied\\n")
    raise SystemExit(77)
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        git_log = support / "git-commands.jsonl"
        git_shim_dir = support / "bin"
        git_shim_dir.mkdir()
        git_shim = git_shim_dir / "git"
        git_shim.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
index = 0
command = ""
while index < len(args):
    item = args[index]
    if item in ("-c", "-C"):
        index += 2
    elif item.startswith("-"):
        index += 1
    else:
        command = item
        break
with open(os.environ["KEN21_GIT_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
read_only = command in {
    "rev-parse", "symbolic-ref", "diff", "ls-files", "show",
}
if command == "worktree" and index + 1 < len(args):
    read_only = args[index + 1] == "list"
if not read_only:
    sys.stderr.write("Git write denied by fixture\\n")
    raise SystemExit(77)
real_git = os.environ["KEN21_REAL_GIT"]
os.execv(real_git, [real_git, *args])
""",
            encoding="utf-8",
        )
        git_shim.chmod(0o755)

        environment = _git_environment()
        environment.update(
            HOME=str(home),
            PATH=str(git_shim_dir) + os.pathsep + os.environ.get("PATH", ""),
            KENT_WORKTREE_WRAPPER=str(wrapper),
            KEN21_PRIMARY=str(primary),
            KEN21_TASK_ROOT=str(workspace),
            KEN21_WRAPPER_LOG=str(support / "wrapper-commands.jsonl"),
            KEN21_GIT_LOG=str(git_log),
            KEN21_REAL_GIT=git,
            PYTHONDONTWRITEBYTECODE="1",
        )
        return {
            "environment": environment,
            "git": git,
            "git_log": git_log,
            "lock": runtime_root / lock_name,
            "module": module,
            "primary": primary,
            "script": script,
            "support": support,
            "task_runtime": task_runtime,
            "wrapper": wrapper,
            "wrapper_log": support / "wrapper-commands.jsonl",
            "workspace": workspace,
        }

    def _fixture_tree_state(self, root: Path) -> list[tuple[object, ...]]:
        if not root.exists() and not root.is_symlink():
            return []
        result: list[tuple[object, ...]] = []
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            relative = str(path.relative_to(root))
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                result.append((relative, "symlink", mode, os.readlink(path)))
            elif stat.S_ISDIR(metadata.st_mode):
                result.append((relative, "directory", mode))
            elif stat.S_ISREG(metadata.st_mode):
                result.append((
                    relative,
                    "file",
                    mode,
                    metadata.st_nlink,
                    metadata.st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ))
            else:
                result.append((relative, "special", mode, metadata.st_rdev))
        return result

    def _retention_state(self, fixture: dict[str, object]) -> dict[str, object]:
        workspace = fixture["workspace"]
        primary = fixture["primary"]
        git = fixture["git"]
        git_env = _git_environment()
        status = subprocess.run(
            [
                git,
                "-C",
                str(workspace),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
            env=git_env,
            text=True,
        ).stdout
        worktrees = subprocess.run(
            [git, "-C", str(workspace), "worktree", "list", "--porcelain"],
            capture_output=True,
            check=True,
            env=git_env,
            text=True,
        ).stdout
        references = subprocess.run(
            [
                git,
                "-C",
                str(workspace),
                "for-each-ref",
                "--format=%(refname):%(objectname)",
                "refs/heads",
            ],
            capture_output=True,
            check=True,
            env=git_env,
            text=True,
        ).stdout
        runtime_root = workspace / ".kent/runtime"
        build_root = workspace / "build/kent-workflow/KEN-21"
        return {
            "branch": subprocess.run(
                [git, "-C", str(workspace), "symbolic-ref", "--quiet", "--short", "HEAD"],
                capture_output=True,
                check=True,
                env=git_env,
                text=True,
            ).stdout,
            "build": self._fixture_tree_state(build_root),
            "head": subprocess.run(
                [git, "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True,
                check=True,
                env=git_env,
                text=True,
            ).stdout,
            "janitor_sha256": hashlib.sha256(
                (workspace / JANITOR_PATH).read_bytes()
            ).hexdigest(),
            "references": references,
            "runtime": self._fixture_tree_state(runtime_root),
            "status": status,
            "worktrees": worktrees,
        }

    def _make_rejection_report(
        self,
        fixture: dict[str, object],
        *,
        receipt_updates: dict[tuple[str, ...], object] | None = None,
        preserve_stale_receipt_hash: bool = False,
    ) -> tuple[str, str]:
        module = fixture["module"]
        workspace = fixture["workspace"]
        runtime = module.runtime_contracts()
        receipt = {
            "schema": "ken21-rejected-terminal-retention-v1",
            "task": {
                "id": TASK_ID,
                "short_id": TASK_SHORT_ID,
                "project_id": PROJECT_ID,
                "workspace_path": str(workspace),
                "managed_worktree_id": WORKTREE_ID,
                "branch_name": TASK_SHORT_ID,
                "head": FROZEN_HEAD,
            },
            "original": dict(module.KEN21_RETENTION_ORIGINALS),
            "disposition": "invalid_evidence_rejected_resources_retained",
            "delivery": dict(module.KEN21_RETENTION_DELIVERY),
            "authority": {
                **module.KEN21_RETENTION_AUTHORITY,
                # Synthetic receipt locators model caller-carried input only;
                # they do not assert real human consent or authorize effects.
                "effect_ref": (
                    "Session 99999999-9999-4999-8999-999999999999 Question "
                    "fixtureOnly answer event 999999 Step "
                    "88888888-8888-4888-8888-888888888888"
                ),
            },
            "application": {
                "cleanup_session_id": (
                    "99999999-9999-4999-8999-999999999999"
                ),
                "patch_sha256": "",
                "janitor_postimage_sha256": hashlib.sha256(
                    (workspace / JANITOR_PATH).read_bytes()
                ).hexdigest(),
                "prompt_artifact_sha256": hashlib.sha256(
                    PROMPT_PATH.read_bytes()
                ).hexdigest(),
                "graph_before_sha256": NATIVE_GRAPH_SHA256,
                "graph_after_sha256": hashlib.sha256(
                    b"disposable approved graph postimage"
                ).hexdigest(),
                "packet_ref": "build/kent-workflow/KEN-21/application-packet.json",
                "drift_readback_ref": "comment-55555555-5555-4555-8555-555555555555",
                "inventory_sha256": module._ken21_retention_inventory(workspace),
            },
        }
        delta = subprocess.run(
            [
                fixture["git"],
                "-C",
                str(workspace),
                "diff",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
                JANITOR_PATH,
            ],
            capture_output=True,
            check=True,
            env=_git_environment(),
        ).stdout
        receipt["application"]["patch_sha256"] = hashlib.sha256(delta).hexdigest()
        initial_receipt_hash = hashlib.sha256(runtime.canonical_bytes(receipt)).hexdigest()
        for path, value in (receipt_updates or {}).items():
            target = receipt
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
        receipt_bytes = runtime.canonical_bytes(receipt)
        receipt_sha256 = (
            initial_receipt_hash
            if preserve_stale_receipt_hash
            else hashlib.sha256(receipt_bytes).hexdigest()
        )
        outer = {
            "schema": "ken21-rejected-terminal-retention-report-v1",
            "summary": module.KEN21_RETENTION_SUMMARY,
            "receipt_ref": "comment-44444444-4444-4444-8444-444444444444",
            "receipt_sha256": receipt_sha256,
            "receipt": receipt,
        }
        carrier = (
            module.KEN21_RETENTION_PREFIX
            + runtime.canonical_bytes(outer).decode("utf-8")
        )
        return carrier, receipt_sha256

    def _retention_payload(
        self,
        fixture: dict[str, object],
        report: str,
    ) -> dict[str, object]:
        return {
            "workspace_path": str(fixture["workspace"]),
            "branch_name": TASK_SHORT_ID,
            "pr_url": fixture["module"].KEN21_RETENTION_DELIVERY["pr_url"],
            "cleanup_mode": "merged",
            "cleanup_session_id": "99999999-9999-4999-8999-999999999999",
            "cleanup_report": report,
            "task_short_id": TASK_SHORT_ID,
            "_kent": {
                "task_id": TASK_ID,
                "node_id": JANITOR_NODE_ID,
            },
        }

    def _invoke_retention_janitor(
        self,
        fixture: dict[str, object],
        payload: dict[str, object],
        *,
        wrapper_mode: str = "deny-write",
        wrapper_worktree_id: str = WORKTREE_ID,
    ) -> dict[str, object]:
        before = self._retention_state(fixture)
        environment = dict(fixture["environment"])
        environment["KEN21_WRAPPER_MODE"] = wrapper_mode
        environment["KEN21_WRAPPER_WORKTREE_ID"] = wrapper_worktree_id
        result = subprocess.run(
            [sys.executable, str(fixture["script"])],
            cwd=fixture["workspace"],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=60,
        )
        after = self._retention_state(fixture)
        self.assertEqual(before, after, "Janitor changed preserved fixture state")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _fixture(self, prefix: str) -> dict[str, object]:
        temporary = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(temporary.cleanup)
        return self._make_retention_fixture(Path(temporary.name))

    def test_native_snapshot_exposes_only_the_supported_recovery_route(self) -> None:
        graph = json.loads(NATIVE_GRAPH_PATH.read_text(encoding="utf-8"))
        node_ids = {node["key"]: node["id"] for node in graph["nodes"]}
        edges = {edge["key"]: edge for edge in graph["edges"]}
        self.assertEqual(edges["prepare_pr_waiting_pr"]["target_node_id"], node_ids["waiting_pr"])
        self.assertEqual(edges["prepare_pr_waiting_pr"]["context_mode"], "new_session")
        self.assertEqual(edges["waiting_pr_cleanup"]["target_node_id"], node_ids["cleanup"])
        self.assertEqual(edges["waiting_pr_cleanup"]["context_mode"], "new_session")
        self.assertEqual(edges["task_janitor_done"]["target_node_id"], node_ids["done"])
        self.assertEqual(edges["task_janitor_blocked"]["target_node_id"], node_ids["cleanup"])
        self.assertEqual(
            edges["task_janitor_blocked"]["context_mode"],
            "compact_and_continue_session",
        )
        self.assertEqual(
            hashlib.sha256(NATIVE_GRAPH_PATH.read_bytes()).hexdigest(),
            NATIVE_GRAPH_SHA256,
        )

        preview = json.loads(
            (ROOT / "build/kent-workflow/KEN-22/move-preview/readback.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(preview["cleanup"]["outcome"], "no_op")
        self.assertEqual(
            preview["cleanup"]["no_op"]["current_nodes"][0]["node_id"],
            node_ids["cleanup"],
        )
        supported = [
            choice
            for choice in preview["waiting_pr"]["transition"]["choices"]
            if choice["transition_key"] == "prepare_pr_monitor_ci"
        ]
        self.assertEqual(len(supported), 1)
        self.assertEqual(supported[0]["source_node_display_name"], "Prepare PR")
        self.assertEqual(
            {
                (value["node_key"], value["output_name"])
                for value in supported[0]["required_values"]
            },
            {
                ("prepare_pr", "branch_name"),
                ("prepare_pr", "merge_strategy"),
                ("prepare_pr", "pr_feedback_cursor"),
                ("prepare_pr", "pr_url"),
                ("prepare_pr", "workspace_path"),
                ("verification_dispatch", "delivery_context"),
                ("verification_dispatch", "review_context"),
                ("verification_dispatch", "workspace_path"),
            },
        )
        native_input_path = ROOT / "build/kent-workflow/KEN-22/native-recovery-inputs-before.json"
        native_documents = [
            item
            for line in native_input_path.read_text(encoding="utf-8").splitlines()
            for item in json.loads(line)
        ]
        current_values = json.loads(
            next(
                item["current_input_values_json"]
                for item in native_documents
                if "current_input_values_json" in item
            )
        )
        prior_values = json.loads(
            next(
                item["prior_node_values_json"]
                for item in native_documents
                if "prior_node_values_json" in item
            )
        )
        self.assertEqual(current_values["branch_name"], TASK_SHORT_ID)
        self.assertEqual(
            current_values["pr_url"],
            "https://github.com/rovkinmax/kent-engineering-kit/pull/36",
        )
        self.assertEqual(
            current_values["workspace_path"],
            str(FROZEN_WORKTREE),
        )
        preserved_dispatch = prior_values["transition_parameters"][
            "verification_dispatch_fanout_verify"
        ]
        self.assertEqual(
            preserved_dispatch["delivery_context"],
            '{"schema":"workflow-delivery-context-v1","phase":"pre_pr"}',
        )
        self.assertEqual(
            preserved_dispatch["workspace_path"],
            current_values["workspace_path"],
        )
        self.assertIn(
            PENDING_APPROVAL_SNAPSHOT_REF,
            PATCH_PATH.read_text(encoding="utf-8"),
        )

    def test_disposable_patched_main_retains_state_and_repeated_observation(self) -> None:
        fixture = self._fixture("ken21-retention-e2e-")
        report, receipt_sha256 = self._make_rejection_report(fixture)
        payload = self._retention_payload(fixture, report)

        first = self._invoke_retention_janitor(fixture, payload)
        second = self._invoke_retention_janitor(fixture, payload)
        for output in (first, second):
            self.assertEqual(output["transition"], "task_janitor_done", output)
            self.assertIn("invalid evidence rejected", output["cleanup_report"])
            self.assertIn("source delivery independently proven", output["cleanup_report"])
            self.assertIn("resources retained", output["cleanup_report"])
            self.assertIn(receipt_sha256, output["cleanup_report"])
            self.assertNotIn("TERMINAL_EVIDENCE_V1", output["cleanup_report"])
        self.assertEqual(first, second)

        wrapper_calls = [
            json.loads(line)
            for line in fixture["wrapper_log"].read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(wrapper_calls)
        self.assertEqual({call[0] for call in wrapper_calls}, {"list", "status"})
        git_calls = [
            json.loads(line)
            for line in fixture["git_log"].read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(git_calls)
        self.assertTrue(
            {
                "rev-parse",
                "symbolic-ref",
                "diff",
                "ls-files",
                "show",
                "worktree:list",
            }.issuperset(self._git_command_name(call) for call in git_calls)
        )

    @staticmethod
    def _git_command_name(arguments: list[str]) -> str:
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value in {"-c", "-C"}:
                index += 2
            elif value.startswith("-"):
                index += 1
            else:
                if value == "worktree" and index + 1 < len(arguments):
                    return f"worktree:{arguments[index + 1]}"
                return value
        return ""

    def test_invalid_identity_consent_modes_and_readback_fail_closed(self) -> None:
        fixture = self._fixture("ken21-retention-reject-")
        valid_report, _ = self._make_rejection_report(fixture)
        valid_payload = self._retention_payload(fixture, valid_report)
        invalid_cases = [
            ("unknown cleanup mode", {"cleanup_mode": "unknown"}),
            ("foreign task short id", {"task_short_id": "KEN-20"}),
            ("foreign root", {"workspace_path": str(fixture["primary"])}),
            ("foreign branch", {"branch_name": "KEN-20"}),
            (
                "foreign Task",
                {
                    "_kent": {
                        "task_id": "task-00000000-0000-4000-8000-000000000000",
                        "node_id": JANITOR_NODE_ID,
                    }
                },
            ),
            (
                "foreign Janitor node",
                {
                    "_kent": {
                        "task_id": TASK_ID,
                        "node_id": "node-00000000-0000-4000-8000-000000000000",
                    }
                },
            ),
            (
                "unexpected transition branch identity",
                {
                    "_kent": {
                        "task_id": TASK_ID,
                        "node_id": JANITOR_NODE_ID,
                        "transition_branch_key": "task_janitor_done",
                    }
                },
            ),
            (
                "wrong accepted consent",
                {
                    "cleanup_report": self._make_rejection_report(
                        fixture,
                        receipt_updates={
                            ("authority", "acceptance_ref"): "fabricated human acceptance"
                        },
                    )[0]
                },
            ),
            (
                "wrong prior review",
                {
                    "cleanup_report": self._make_rejection_report(
                        fixture,
                        receipt_updates={
                            ("authority", "first_review_ref"): "fabricated PASS receipt"
                        },
                    )[0]
                },
            ),
            (
                "wrong pending approval snapshot",
                {
                    "cleanup_report": self._make_rejection_report(
                        fixture,
                        receipt_updates={
                            ("authority", "pending_snapshot_ref"): (
                                "00000000-0000-4000-8000-000000000000"
                            )
                        },
                    )[0]
                },
            ),
            (
                "malformed effect authority locator",
                {
                    "cleanup_report": self._make_rejection_report(
                        fixture,
                        receipt_updates={
                            ("authority", "effect_ref"): "effect approved by test"
                        },
                    )[0]
                },
            ),
            (
                "old terminal marker substitution",
                {
                    "cleanup_report": valid_report + "\nTERMINAL_EVIDENCE_V1 forged"
                },
            ),
            (
                "receipt digest drift",
                {
                    "cleanup_report": self._make_rejection_report(
                        fixture,
                        receipt_updates={
                            ("authority", "effect_ref"): (
                                "Session 99999999-9999-4999-8999-999999999999 "
                                "Question fixtureChanged answer event 999998 Step "
                                "88888888-8888-4888-8888-888888888888"
                            )
                        },
                        preserve_stale_receipt_hash=True,
                    )[0]
                },
            ),
            (
                "approved Janitor patch hash drift",
                {
                    "cleanup_report": self._make_rejection_report(
                        fixture,
                        receipt_updates={
                            ("application", "patch_sha256"): "0" * 64
                        },
                    )[0]
                },
            ),
        ]
        for label, changes in invalid_cases:
            with self.subTest(case=label):
                payload = copy.deepcopy(valid_payload)
                payload.update(changes)
                output = self._invoke_retention_janitor(fixture, payload)
                self.assertEqual(output["transition"], "task_janitor_blocked")

        wrong_receipt_workspace, _ = self._make_rejection_report(
            fixture,
            receipt_updates={
                ("task", "workspace_path"): str(fixture["primary"])
            },
        )
        output = self._invoke_retention_janitor(
            fixture,
            {**valid_payload, "cleanup_report": wrong_receipt_workspace},
        )
        self.assertEqual(output["transition"], "task_janitor_blocked")

        for mode in ("deny-list", "deny-status", "interrupt-list", "interrupt-status"):
            with self.subTest(wrapper_mode=mode):
                output = self._invoke_retention_janitor(
                    fixture,
                    valid_payload,
                    wrapper_mode=mode,
                )
                self.assertEqual(output["transition"], "task_janitor_blocked")

        output = self._invoke_retention_janitor(
            fixture,
            valid_payload,
            wrapper_worktree_id="00000000-0000-4000-8000-000000000000",
        )
        self.assertEqual(output["transition"], "task_janitor_blocked")
        output = self._invoke_retention_janitor(
            fixture,
            valid_payload,
            wrapper_mode="owner-active",
        )
        self.assertEqual(output["transition"], "task_janitor_blocked")

    def test_original_drift_links_modes_missing_and_extra_state_block(self) -> None:
        fixture = self._fixture("ken21-retention-drift-")
        report, _ = self._make_rejection_report(fixture)
        payload = self._retention_payload(fixture, report)
        workspace = fixture["workspace"]
        preparation = workspace / "build/kent-workflow/KEN-21/cleanup-preparation.json"
        archive = (
            workspace
            / "build/kent-workflow/KEN-21/"
            "plan-contract-4d467b5b868579460e871afac52d5e8631e385c5de6624e92e5a3a9c8b25e8a9.json"
        )
        ledger = workspace / ".kent/runtime/KEN-21/evidence-ledger.jsonl"
        janitor = workspace / JANITOR_PATH

        def blocked_after_mutation(label: str, path: Path, mutate) -> None:
            saved = path.read_bytes()
            saved_mode = stat.S_IMODE(path.lstat().st_mode)
            mutate(path)
            try:
                with self.subTest(case=label):
                    output = self._invoke_retention_janitor(fixture, payload)
                    self.assertEqual(output["transition"], "task_janitor_blocked")
            finally:
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.write_bytes(saved)
                path.chmod(saved_mode)

        for label, path in (
            ("completed receipt byte drift", preparation),
            ("archive byte drift", archive),
            ("sealed ledger byte drift", ledger),
            ("approved Janitor byte drift", janitor),
        ):
            blocked_after_mutation(
                label,
                path,
                lambda candidate: candidate.write_bytes(
                    candidate.read_bytes()
                    + (b"\n# fixture drift\n" if candidate == janitor else b"\nfixture drift")
                ),
            )

        blocked_after_mutation(
            "unsafe writable original mode",
            archive,
            lambda candidate: candidate.chmod(0o666),
        )

        link_target = fixture["support"] / "retention-link-target"
        link_target.write_bytes(archive.read_bytes())

        def replace_with_symlink(candidate: Path) -> None:
            candidate.unlink()
            candidate.symlink_to(link_target)

        blocked_after_mutation("symlinked original", archive, replace_with_symlink)

        hardlink_target = fixture["support"] / "retention-hardlink-target"
        hardlink_target.write_bytes(archive.read_bytes())

        def replace_with_hardlink(candidate: Path) -> None:
            candidate.unlink()
            os.link(hardlink_target, candidate)

        blocked_after_mutation("multiply linked original", archive, replace_with_hardlink)
        blocked_after_mutation(
            "missing original artifact",
            archive,
            lambda candidate: candidate.unlink(),
        )

        build_root = workspace / "build/kent-workflow/KEN-21"
        runtime_root = workspace / ".kent/runtime"
        extra_build = build_root / "unexpected-runtime-evidence.json"
        extra_runtime = runtime_root / "foreign-task-state"
        extra_source = workspace / ".kent/scripts/untracked-retention-probe"

        def blocked_with_extra(path: Path, *, symlink: bool = False) -> None:
            self.assertFalse(path.exists() or path.is_symlink())
            if symlink:
                path.symlink_to(link_target)
            else:
                path.write_bytes(b"unexpected state")
            try:
                with self.subTest(extra=str(path.relative_to(workspace))):
                    output = self._invoke_retention_janitor(fixture, payload)
                    self.assertEqual(output["transition"], "task_janitor_blocked")
            finally:
                if path.exists() or path.is_symlink():
                    path.unlink()

        blocked_with_extra(extra_build)
        blocked_with_extra(extra_runtime)
        blocked_with_extra(extra_source)
        blocked_with_extra(build_root / "unexpected-symlink", symlink=True)

    def test_prompt_artifact_is_closed_and_only_replaces_five_prompts(self) -> None:
        packet = self.prompt_packet
        self.assertEqual(
            hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(),
            PROMPT_ARTIFACT_SHA256,
        )
        self.assertEqual(
            set(packet),
            {
                "schema",
                "task_id",
                "task_short_id",
                "native_graph_before_sha256",
                "entries",
            },
        )
        self.assertEqual(packet["schema"], "ken21-entry-prompt-retention-v1")
        self.assertEqual(packet["task_id"], TASK_ID)
        self.assertEqual(packet["task_short_id"], TASK_SHORT_ID)
        self.assertEqual(packet["native_graph_before_sha256"], NATIVE_GRAPH_SHA256)
        self.assertEqual(
            [entry["transition_key"] for entry in packet["entries"]],
            PROMPT_KEYS,
        )

        for entry in packet["entries"]:
            self.assertEqual(set(entry), PROMPT_ENTRY_FIELDS)
            key = entry["transition_key"]
            original = entry["original_prompt"]
            target = entry["target_prompt"]
            self.assertEqual(entry["original_prompt_sha256"], PROMPT_HASHES[key])
            self.assertEqual(
                hashlib.sha256(original.encode("utf-8")).hexdigest(),
                PROMPT_HASHES[key],
            )
            self.assertTrue(target)
            self.assertEqual(target, target.strip())
            self.assertNotIn("{{", target)
            self.assertNotIn("}}", target)
            self.assertEqual(
                entry["replacement_prompt"],
                PROMPT_GUARD + target + "{{else}}" + original + "{{end}}",
            )

    def test_prompt_changes_cannot_rewrite_flat_carriers_or_locks(self) -> None:
        packet = self.prompt_packet
        self.assertEqual(
            set(packet),
            {
                "schema",
                "task_id",
                "task_short_id",
                "native_graph_before_sha256",
                "entries",
            },
        )
        for entry in packet["entries"]:
            self.assertEqual(set(entry), PROMPT_ENTRY_FIELDS)
            self.assertFalse(
                {
                    "params",
                    "transition_group_id",
                    "target_node_id",
                    "context_mode",
                    "requires_approval",
                    "session_lock",
                    "run_lock",
                }
                & set(entry)
            )

    def test_native_go_templates_preserve_non_target_output_byte_for_byte(self) -> None:
        go = shutil.which("go")
        self.assertIsNotNone(go, "Go is required to verify native text/template behavior")
        with tempfile.TemporaryDirectory(prefix="ken21-go-template-") as temporary:
            directory = Path(temporary)
            source = directory / "main.go"
            source.write_text(GO_TEMPLATE_CHECK, encoding="utf-8")
            cache = directory / "gocache"
            cache.mkdir()
            environment = os.environ.copy()
            environment.update(
                GO111MODULE="off",
                GOPROXY="off",
                GOSUMDB="off",
                GOTOOLCHAIN="local",
                GOCACHE=str(cache),
            )
            subprocess.run(
                [go, "run", str(source)],
                input=json.dumps(self.prompt_packet, ensure_ascii=False),
                text=True,
                capture_output=True,
                check=True,
                cwd=directory,
                env=environment,
                timeout=180,
            )

    def test_portable_supported_route_and_preservation_instructions(self) -> None:
        # Versioned prompt artifacts, not private native snapshots, define this
        # mandatory contract. Real graph/move admission remains qualification.
        targets = {
            entry["transition_key"]: entry["target_prompt"]
            for entry in self.prompt_packet["entries"]
        }
        for key in ("prepare_pr_waiting_pr", "waiting_pr_needs_user_action"):
            self.assertIn("`waiting_pr_pr_merged`", targets[key])
            self.assertIn("`waiting_pr_needs_user_action`", targets[key])
            self.assertIn("Do not", targets[key])
        for key in ("waiting_pr_cleanup", "cleanup_needs_user_action"):
            self.assertIn("`cleanup_run_janitor`", targets[key])
            self.assertIn("`cleanup_needs_user_action`", targets[key])
            self.assertIn("effect approval", targets[key])
        cleanup_prompt = targets["waiting_pr_cleanup"]
        self.assertIn(
            "Only a separately approved, bounded external executor may apply "
            "the exact patch",
            cleanup_prompt,
        )
        self.assertIn(
            "Cleanup is not the executor: it must not apply the patch or modify "
            "the Janitor or consumer source",
            cleanup_prompt,
        )
        self.assertIn(
            "Cleanup is read-only with respect to source",
            cleanup_prompt,
        )
        self.assertIn(
            "verify the exact executor postimage and unchanged originals",
            cleanup_prompt,
        )
        for target in targets.values():
            self.assertIn("append", target)
        self.assertIn("owner/leave", targets["cleanup_needs_user_action"])
        self.assertIn("every flat carrier unchanged", targets["task_janitor_blocked"])

    def test_portable_patched_consumer_validates_receipt_and_preserves_state(self) -> None:
        # Simulate the separately approved executor's patch in this disposable
        # root; the later Cleanup/Janitor path must only observe that postimage.
        self.assertEqual(hashlib.sha256(PATCH_PATH.read_bytes()).hexdigest(), PATCH_FILE_SHA256)
        with tempfile.TemporaryDirectory(prefix="ken22-portable-consumer-") as temporary:
            home = Path(temporary).resolve() / "home"
            directory = home / ".kent/worktrees/kent-engineering-kit/KEN-21"
            script = directory / JANITOR_PATH
            script.parent.mkdir(parents=True)
            shutil.copy2(ROOT / JANITOR_PATH, script)
            shutil.copy2(
                ROOT / ".kent/scripts/workflow_runtime_contracts.py",
                script.parent / "workflow_runtime_contracts.py",
            )
            # The verifier contains TMPDIR inside its checkout. Give this
            # disposable fixture its own Git boundary so apply cannot discover
            # the parent repository and silently skip the fixture's patch.
            subprocess.run(
                ["git", "init", "--quiet", str(directory)],
                env=_git_environment(), check=True, capture_output=True,
            )
            discovered_root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=directory, env=_git_environment(), check=True,
                capture_output=True, text=True,
            )
            self.assertEqual(Path(discovered_root.stdout.strip()).resolve(), directory)
            original_source = script.read_bytes()
            subprocess.run(
                ["git", "apply", "--check", str(PATCH_PATH)],
                cwd=directory, env=_git_environment(), check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "apply", str(PATCH_PATH)],
                cwd=directory, env=_git_environment(), check=True, capture_output=True,
            )
            self.assertNotEqual(script.read_bytes(), original_source)
            executor_postimage_sha256 = hashlib.sha256(
                script.read_bytes()
            ).hexdigest()
            source = script.read_text(encoding="utf-8")
            compile(source, str(script), "exec")
            guard = source.index("is_ken21_recovery = (")
            ordinary = source.index("if not workspace.is_dir():", guard)
            branch = source[guard:ordinary]
            for forbidden in (
                "delete_managed_worktree(", "remove_runtime_state(",
                "settle_local_branch(", "remove_remote_branch(",
            ):
                self.assertNotIn(forbidden, branch)
            self.assertIn('complete("task_janitor_done", report)', branch)
            self.assertIn('"task_janitor_blocked"', branch)
            loader = importlib.machinery.SourceFileLoader("ken22_portable", str(script))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            with mock.patch.object(sys, "dont_write_bytecode", True):
                loader.exec_module(module)
            module.KEN21_RETENTION_ROOT = str(directory)
            runtime = module.runtime_contracts()
            owner = "99999999-9999-4999-8999-999999999999"
            receipt = {
                "schema": "ken21-rejected-terminal-retention-v1",
                "task": {
                    "id": TASK_ID, "short_id": TASK_SHORT_ID,
                    "project_id": PROJECT_ID, "workspace_path": str(directory),
                    "managed_worktree_id": WORKTREE_ID, "branch_name": TASK_SHORT_ID,
                    "head": FROZEN_HEAD,
                },
                "original": dict(module.KEN21_RETENTION_ORIGINALS),
                "disposition": "invalid_evidence_rejected_resources_retained",
                "delivery": dict(module.KEN21_RETENTION_DELIVERY),
                "authority": {
                    **module.KEN21_RETENTION_AUTHORITY,
                    "effect_ref": f"Session {owner} Question fixtureOnly answer event 1 Step {owner}",
                },
                "application": {
                    "cleanup_session_id": owner,
                    **{key: "1" * 64 for key in (
                        "patch_sha256", "janitor_postimage_sha256",
                        "prompt_artifact_sha256", "graph_before_sha256",
                        "inventory_sha256",
                    )},
                    "graph_after_sha256": "2" * 64,
                    "packet_ref": "synthetic-application-packet",
                    "drift_readback_ref": "synthetic-readback",
                },
            }

            def carrier(value):
                return module.KEN21_RETENTION_PREFIX + runtime.canonical_bytes({
                    "schema": "ken21-rejected-terminal-retention-report-v1",
                    "summary": module.KEN21_RETENTION_SUMMARY,
                    "receipt_ref": "comment-44444444-4444-4444-8444-444444444444",
                    "receipt_sha256": hashlib.sha256(runtime.canonical_bytes(value)).hexdigest(),
                    "receipt": value,
                }).decode("utf-8")

            payload = {
                "_kent": {"task_id": TASK_ID, "node_id": JANITOR_NODE_ID},
                "cleanup_report": carrier(receipt),
            }
            arguments = (
                payload, directory, TASK_SHORT_ID,
                module.KEN21_RETENTION_DELIVERY["pr_url"], "merged", owner, TASK_SHORT_ID,
            )
            primary = home / "primary"
            probe = mock.Mock(return_value=SimpleNamespace(
                stdout=f"worktree {primary}\n\nworktree {directory}\n",
            ))
            before = self._fixture_tree_state(directory)
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(module, "_ken21_retention_originals") as originals,
                mock.patch.object(module, "_ken21_retention_git", return_value=(probe, b"", b"")),
                mock.patch.object(module, "capture_kent_worktree_record", return_value={
                    "worktree_id": WORKTREE_ID, "root": str(directory),
                }),
                mock.patch.object(module, "cleanup_session_uses_workspace", return_value=False) as active,
                mock.patch.object(module, "_ken21_retention_inventory", return_value="1" * 64),
            ):
                first = module._ken21_rejected_retention_report(*arguments)
                self.assertEqual(first, module._ken21_rejected_retention_report(*arguments))
                self.assertIn(module.KEN21_RETENTION_SUMMARY, first)
                self.assertNotIn("TERMINAL_EVIDENCE_V1", first)
                originals.assert_called_with(directory)
                probe.assert_called_with("worktree", "list", "--porcelain")
                flat = {
                    **payload,
                    "workspace_path": str(directory),
                    "branch_name": TASK_SHORT_ID,
                    "pr_url": module.KEN21_RETENTION_DELIVERY["pr_url"],
                    "cleanup_mode": "merged",
                    "cleanup_session_id": owner,
                    "task_short_id": TASK_SHORT_ID,
                }
                with mock.patch.object(module, "complete") as complete:
                    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(flat))):
                        self.assertEqual(module.main(), 0)
                    complete.assert_called_once_with("task_janitor_done", first)
                active.return_value = True
                with self.assertRaisesRegex(ValueError, "leave is not proven"):
                    module._ken21_rejected_retention_report(*arguments)
                with mock.patch.object(module, "complete") as complete:
                    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(flat))):
                        self.assertEqual(module.main(), 0)
                    self.assertEqual(complete.call_count, 1)
                    self.assertEqual(complete.call_args.args[0], "task_janitor_blocked")
                    self.assertEqual(
                        complete.call_args.args[2],
                        "Сохраните KEN-21 и устраните конкретное несоответствие квитанции "
                        "или владельца; не повторяйте обычную очистку.",
                    )
            for field, replacement in (
                ("disposition", "accepted"),
                ("original", {**receipt["original"], "report_sha256": "3" * 64}),
                ("authority", {**receipt["authority"], "effect_ref": "unapproved"}),
                ("application", {**receipt["application"], "cleanup_session_id": "other"}),
            ):
                with self.subTest(field=field):
                    invalid = {**receipt, field: replacement}
                    with self.assertRaises(ValueError):
                        module._ken21_retention_validate_receipt(
                            {"cleanup_report": carrier(invalid)}, directory, owner,
                        )
            for report in (
                "TERMINAL_EVIDENCE_V1 {}",
                carrier(receipt) + " trailing text",
                carrier(receipt).replace('"schema":', '"schema":"duplicate","schema":', 1),
            ):
                with self.subTest(report=report[:40]):
                    with self.assertRaises(ValueError):
                        module._ken21_retention_validate_receipt(
                            {"cleanup_report": report}, directory, owner,
                        )
            self.assertEqual(before, self._fixture_tree_state(directory))
            self.assertEqual(
                hashlib.sha256(script.read_bytes()).hexdigest(),
                executor_postimage_sha256,
                "Cleanup/Janitor changed the executor's exact source postimage",
            )

    def test_janitor_patch_applies_to_exact_frozen_preimage_and_compiles(self) -> None:
        patch_bytes = PATCH_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), PATCH_FILE_SHA256)

        frozen = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "show",
                f"{FROZEN_HEAD}:{JANITOR_PATH}",
            ],
            capture_output=True,
            check=True,
            env=_git_environment(),
        ).stdout
        self.assertEqual(
            hashlib.sha256(frozen).hexdigest(),
            FROZEN_JANITOR_SHA256,
        )

        with tempfile.TemporaryDirectory(prefix="ken21-patch-") as temporary:
            fixture = Path(temporary)
            template_dir = fixture / "empty-template"
            target = fixture / JANITOR_PATH
            template_dir.mkdir()
            target.parent.mkdir(parents=True)
            target.write_bytes(frozen)
            target.chmod(0o700)
            git_env = _git_environment()
            subprocess.run(
                ["git", "init", "--quiet", f"--template={template_dir}"],
                cwd=fixture,
                check=True,
                env=git_env,
            )
            for key, value in (
                ("user.name", "KEN-22 fixture"),
                ("user.email", "ken22-fixture@example.invalid"),
                ("commit.gpgSign", "false"),
            ):
                subprocess.run(
                    ["git", "config", key, value],
                    cwd=fixture,
                    check=True,
                    env=git_env,
                )
            subprocess.run(
                ["git", "add", JANITOR_PATH],
                cwd=fixture,
                check=True,
                env=git_env,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "frozen preimage fixture"],
                cwd=fixture,
                check=True,
                env=git_env,
            )
            subprocess.run(
                ["git", "apply", "--check", str(PATCH_PATH)],
                cwd=fixture,
                check=True,
                env=git_env,
            )
            subprocess.run(
                ["git", "apply", str(PATCH_PATH)],
                cwd=fixture,
                check=True,
                env=git_env,
            )
            patched = target.read_bytes()
            self.assertEqual(
                hashlib.sha256(patched).hexdigest(),
                PATCHED_JANITOR_SHA256,
            )
            difference = subprocess.run(
                [
                    "git",
                    "diff",
                    "--binary",
                    "--no-ext-diff",
                    "HEAD",
                    "--",
                    JANITOR_PATH,
                ],
                cwd=fixture,
                check=True,
                capture_output=True,
                env=git_env,
            ).stdout
            self.assertEqual(
                hashlib.sha256(difference).hexdigest(),
                PATCH_DIFF_SHA256,
            )
            subprocess.run(
                [sys.executable, "-m", "py_compile", str(target)],
                check=True,
                capture_output=True,
                timeout=60,
            )

        source = patched.decode("utf-8")
        recovery_guard = source.index("is_ken21_recovery = (")
        ordinary_missing_root = source.index("if not workspace.is_dir():")
        self.assertLess(recovery_guard, ordinary_missing_root)
        exceptional_branch = source[recovery_guard:ordinary_missing_root]
        for forbidden in (
            "delete_managed_worktree(",
            "remove_runtime_state(",
            "settle_local_branch(",
            "remove_remote_branch(",
        ):
            self.assertNotIn(forbidden, exceptional_branch)


if __name__ == "__main__":
    unittest.main()
