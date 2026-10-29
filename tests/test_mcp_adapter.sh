#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
call_adapter="$repo_root/adapters/mcp/kent-mcp-call"
list_adapter="$repo_root/adapters/mcp/kent-mcp-list"
tmp="$(mktemp -d)"
tmp="$(cd "$tmp" && pwd -P)"
trap 'rm -rf "$tmp"' EXIT

home="$tmp/home"
fake_bin="$tmp/bin"
main="$tmp/Example"
worktree="$tmp/TASK-1"
r2_worktree="$tmp/TASK-R2"
memory_bin="$home/.kent/bin"
mkdir -p "$memory_bin" "$home/.mcporter" "$fake_bin" "$main"

git -C "$main" init -q
git -C "$main" config user.name "Kent Test"
git -C "$main" config user.email "kent@example.invalid"
echo tracked >"$main/tracked"
git -C "$main" add tracked
git -C "$main" commit -qm "Initial"
git -C "$main" worktree add -qb task "$worktree"
git -C "$main" worktree add --detach "$r2_worktree" HEAD

cat >"$main/.mcp.json" <<'JSON'
{
  "mcpServers": {
    "example": {
      "command": "example-server"
    },
    "mobile": {
      "command": "project-mobile"
    }
  }
}
JSON

cat >"$home/.mcporter/mcporter.json" <<'JSON'
{
  "mcpServers": {
    "mobile": {
      "command": "npx",
      "args": [
        "-y",
        "claude-in-mobile@latest"
      ]
    }
  }
}
JSON

cat >"$fake_bin/mcporter" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
: "${MCPORTER_ARGS_LOG:?}"
printf '%s\n' "$@" >"$MCPORTER_ARGS_LOG"
if [[ -n "${MCPORTER_CALL_COUNT_FILE:-}" ]]; then
  printf 'call\n' >>"$MCPORTER_CALL_COUNT_FILE"
fi
if [[ -n "${MCPORTER_STDERR_FILE:-}" ]]; then
  cat "$MCPORTER_STDERR_FILE" >&2
fi
if [[ -n "${MCPORTER_FIXTURE_FILE:-}" ]]; then
  cat "$MCPORTER_FIXTURE_FILE"
else
  printf '%s\n' '{"ok":true}'
fi
exit "${MCPORTER_EXIT_CODE:-0}"
SH
chmod +x "$fake_bin/mcporter"

cat >"$memory_bin/kent-project-memory" <<'PY'
#!/usr/bin/env python3
"""Faithful local fixture for the narrow r2-mcp-request-v1 wire protocol."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid

MAX_BODY = 8 * 1024 * 1024
MAX_HEADER = 64 * 1024
home = Path(os.environ["HOME"])
control = home / ".kent"
expected_binding = json.loads((control / "test-binding.json").read_text())
state = Path(expected_binding["project_path"])
task_root = state / "tasks" / expected_binding["task_key"]
object_root = task_root / "artifacts" / "mcp" / "objects"
call_root = task_root / "artifacts" / "mcp" / "calls"
cache_root = task_root / "artifacts" / "mcp" / "cache"
trace = control / "mcp-protocol.jsonl"


def fail(message, code=2):
    print(f"fake_memory:{message}", file=sys.stderr)
    raise SystemExit(code)

if sys.argv[1:] != ["mcp"]:
    fail("invalid_adapter_command")


def write_atomic(path, raw):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    temporary.write_bytes(raw)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def response(result):
    print(json.dumps({"schema": "r2-mcp-response-v1", "result": result},
                     sort_keys=True, separators=(",", ":")))


line = sys.stdin.buffer.readline(MAX_HEADER + 1)
if len(line) > MAX_HEADER or not line.endswith(b"\n"):
    fail("invalid_header")
try:
    request = json.loads(line)
except (UnicodeError, ValueError):
    fail("invalid_json")
if (not isinstance(request, dict)
        or request.get("schema") != "r2-mcp-request-v1"
        or not isinstance(request.get("operation"), str)):
    fail("invalid_request")

operation = request["operation"]
body = b""
if operation == "put-artifact":
    body = sys.stdin.buffer.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        fail("artifact_too_large")
elif sys.stdin.buffer.read(1):
    fail("unexpected_body")

logged = dict(request)
logged.pop("owner_token", None)
with trace.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(logged, sort_keys=True, separators=(",", ":")) + "\n")


def verify_workspace():
    workspace = request.get("workspace_path")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        fail("invalid_workspace")
    return Path(workspace)


def verify_binding():
    verify_workspace()
    if request.get("binding_ref") != {
            "path": str(task_root / "task.json"),
            "sha256": expected_binding["task_binding_sha256"]}:
        fail("binding_ref_mismatch")
    supplied = request.get("standalone_binding")
    if supplied is not None and supplied != expected_binding:
        fail("standalone_binding_mismatch")


def checked_metadata(value):
    expected = {"operation", "server", "tool", "config_sha256",
                "output_mode", "safe_mode", "save_raw"}
    if not isinstance(value, dict) or set(value) != expected:
        fail("invalid_permitted_metadata")
    if value["operation"] not in ("list", "call"):
        fail("invalid_permitted_metadata")
    for name in ("server", "tool", "output_mode"):
        item = value[name]
        if (not isinstance(item, str) or len(item) > 128
                or (item and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item))):
            fail("invalid_permitted_metadata")
    if not re.fullmatch(r"[0-9a-f]{64}", value["config_sha256"]):
        fail("invalid_permitted_metadata")
    if type(value["safe_mode"]) is not bool or type(value["save_raw"]) is not bool:
        fail("invalid_permitted_metadata")


def invocation_paths(invocation_id):
    if (not isinstance(invocation_id, str)
            or str(uuid.UUID(invocation_id)) != invocation_id):
        fail("invalid_invocation_id")
    directory = call_root / invocation_id
    return directory / "started.json", directory / "finished.json"


def load_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def verify_owner(started, owner_token):
    if (not isinstance(owner_token, str)
            or started is None
            or not secrets.compare_digest(
                started["owner_sha256"],
                hashlib.sha256(owner_token.encode()).hexdigest())):
        fail("invocation_owner_mismatch", 3)


mode = (control / "mcp-mode").read_text().strip()
if operation == "resolve-mcp":
    verify_workspace()
    if mode == "legacy":
        response({"mode": "legacy"})
    elif mode == "r2":
        supplied = request.get("standalone_binding")
        if supplied != expected_binding or os.environ.get("KENT_TASK_ID", ""):
            fail("standalone_binding_required")
        response({
            "mode": "r2",
            "binding_ref": {
                "path": str(task_root / "task.json"),
                "sha256": expected_binding["task_binding_sha256"],
            },
        })
    else:
        fail("unknown_mode")
elif operation == "new-invocation":
    verify_binding()
    invocation_id = str(uuid.uuid4())
    started, finished = invocation_paths(invocation_id)
    response({"invocation_id": invocation_id,
              "started_path": str(started), "finished_path": str(finished)})
elif operation == "begin-invocation":
    verify_binding()
    invocation_id = request.get("invocation_id")
    started_path, finished_path = invocation_paths(invocation_id)
    request_sha = request.get("request_sha256")
    metadata = request.get("permitted_metadata")
    if not re.fullmatch(r"[0-9a-f]{64}", str(request_sha)):
        fail("invalid_request_sha256")
    checked_metadata(metadata)
    started = load_json(started_path)
    finished = load_json(finished_path)
    if started is not None:
        if (started["request_sha256"] != request_sha
                or started["permitted_metadata"] != metadata):
            fail("invocation_request_conflict", 3)
        response({"state": "finished" if finished is not None else "unknown",
                  "receipt_path": str(finished_path if finished is not None else started_path)})
    elif (control / "force-unknown-id").exists() and (
            (control / "force-unknown-id").read_text().strip() == invocation_id):
        owner_token = secrets.token_hex(32)
        write_atomic(started_path, json.dumps({
            "request_sha256": request_sha,
            "permitted_metadata": metadata,
            "owner_sha256": hashlib.sha256(owner_token.encode()).hexdigest(),
            "object_sha256": None,
        }, sort_keys=True).encode())
        response({"state": "unknown", "receipt_path": str(started_path)})
    else:
        owner_token = secrets.token_hex(32)
        write_atomic(started_path, json.dumps({
            "request_sha256": request_sha,
            "permitted_metadata": metadata,
            "owner_sha256": hashlib.sha256(owner_token.encode()).hexdigest(),
            "object_sha256": None,
        }, sort_keys=True).encode())
        response({"state": "started", "owner_token": owner_token,
                  "receipt_path": str(started_path)})
elif operation == "put-artifact":
    verify_binding()
    invocation_id = request.get("invocation_id")
    started_path, _ = invocation_paths(invocation_id)
    started = load_json(started_path)
    verify_owner(started, request.get("owner_token"))
    kind = request.get("kind")
    digest = request.get("sha256")
    size = request.get("byte_size")
    if (kind not in ("raw", "schema") or type(size) is not int or size != len(body)
            or hashlib.sha256(body).hexdigest() != digest):
        fail("artifact_digest_mismatch")
    object_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    object_path = object_root / digest
    if object_path.exists():
        if object_path.read_bytes() != body:
            fail("artifact_conflict", 3)
    else:
        write_atomic(object_path, body)
    started["object_sha256"] = digest
    write_atomic(started_path, json.dumps(started, sort_keys=True).encode())
    response({"object_sha256": digest, "object_path": str(object_path)})
elif operation == "finish-invocation":
    verify_binding()
    invocation_id = request.get("invocation_id")
    started_path, finished_path = invocation_paths(invocation_id)
    started = load_json(started_path)
    verify_owner(started, request.get("owner_token"))
    object_sha = request.get("object_sha256")
    if object_sha is not None and not (object_root / object_sha).is_file():
        fail("artifact_missing")
    finished = {
        "exit_code": request.get("exit_code"),
        "error_code": request.get("error_code"),
        "object_sha256": object_sha,
        "permitted_metadata": started["permitted_metadata"],
    }
    write_atomic(finished_path, json.dumps(finished, sort_keys=True).encode())
    response({"state": "finished", "receipt_path": str(finished_path)})
elif operation == "read-invocation":
    verify_binding()
    invocation_id = request.get("invocation_id")
    started_path, finished_path = invocation_paths(invocation_id)
    started = load_json(started_path)
    finished = load_json(finished_path)
    if started is None:
        result = {"state": "not-started"}
    elif finished is None:
        result = {"state": "unknown",
                  "permitted_metadata": started["permitted_metadata"]}
    else:
        result = {"state": "finished", **finished,
                  "receipt_path": str(finished_path)}
        if finished["object_sha256"] is not None:
            result["object_path"] = str(object_root / finished["object_sha256"])
    response(result)
elif operation == "read-cache":
    verify_binding()
    cache_key = request.get("cache_key")
    if not re.fullmatch(r"[0-9a-f]{64}", str(cache_key)):
        fail("invalid_cache_key")
    cache = load_json(cache_root / f"{cache_key}.json")
    if cache is None:
        response({"state": "miss"})
    else:
        path = object_root / cache["object_sha256"]
        response({"state": "hit", "object_sha256": cache["object_sha256"],
                  "object_path": str(path)})
elif operation == "publish-cache":
    verify_binding()
    invocation_id = request.get("invocation_id")
    started_path, _ = invocation_paths(invocation_id)
    started = load_json(started_path)
    verify_owner(started, request.get("owner_token"))
    cache_key = request.get("cache_key")
    object_sha = request.get("object_sha256")
    if (not re.fullmatch(r"[0-9a-f]{64}", str(cache_key))
            or not re.fullmatch(r"[0-9a-f]{64}", str(object_sha))
            or not (object_root / object_sha).is_file()):
        fail("invalid_cache_publication")
    cache = cache_root / f"{cache_key}.json"
    write_atomic(cache, json.dumps({"object_sha256": object_sha},
                                   sort_keys=True).encode())
    response({"state": "published"})
else:
    fail("unsupported_operation")
PY
chmod +x "$memory_bin/kent-project-memory"
echo legacy >"$home/.kent/mcp-mode"
r2_project_path="$tmp/R2/kent-engineering-kit"
standalone_binding="$(
  jq -cn \
    --arg project_path "$r2_project_path" \
    --arg project_uid "project-11111111-1111-4111-8111-111111111111" \
    --arg task_key "intake-22222222-2222-4222-8222-222222222222" \
    --arg task_uid "task-33333333-3333-4333-8333-333333333333" \
    --arg task_binding_sha256 "$(printf 'b%.0s' {1..64})" \
    '{project_path:$project_path,project_uid:$project_uid,task_key:$task_key,
      task_uid:$task_uid,task_binding_sha256:$task_binding_sha256}'
)"
printf '%s\n' "$standalone_binding" >"$home/.kent/test-binding.json"
call_count_file="$tmp/mcporter-calls"
: >"$call_count_file"
: >"$home/.kent/mcp-protocol.jsonl"

r2_memory_request() {
  HOME="$home" KENT_TASK_ID= "$memory_bin/kent-project-memory" mcp
}

new_r2_invocation() {
  local resolve_request resolved binding_ref allocation_request
  resolve_request="$(
    jq -cn --arg workspace "$r2_worktree" \
      --argjson standalone "$standalone_binding" \
      '{schema:"r2-mcp-request-v1",operation:"resolve-mcp",
        workspace_path:$workspace,standalone_binding:$standalone}'
  )"
  resolved="$(printf '%s\n' "$resolve_request" | r2_memory_request)"
  binding_ref="$(jq -ce '.result.binding_ref | select(type == "object")' \
    <<<"$resolved")"
  allocation_request="$(
    jq -cn --arg workspace "$r2_worktree" \
      --argjson binding_ref "$binding_ref" \
      --argjson standalone "$standalone_binding" \
      '{schema:"r2-mcp-request-v1",operation:"new-invocation",
        workspace_path:$workspace,binding_ref:$binding_ref,
        standalone_binding:$standalone}'
  )"
  printf '%s\n' "$allocation_request" | r2_memory_request
}

run_adapter() {
  local log="$1"
  shift
  (
    cd "$worktree"
    HOME="$home" \
      PATH="$fake_bin:$PATH" \
      MCPORTER_ARGS_LOG="$log" \
      MCPORTER_FIXTURE_FILE="${MCPORTER_FIXTURE_FILE:-}" \
      MCPORTER_STDERR_FILE="${MCPORTER_STDERR_FILE:-}" \
      MCPORTER_EXIT_CODE="${MCPORTER_EXIT_CODE:-0}" \
      MCPORTER_CALL_COUNT_FILE="${MCPORTER_CALL_COUNT_FILE:-}" \
      "$@"
  )
}

run_r2_adapter() {
  local log="$1"
  shift
  (
    cd "$r2_worktree"
    HOME="$home" \
      PATH="$fake_bin:$PATH" \
      MCPORTER_ARGS_LOG="$log" \
      MCPORTER_FIXTURE_FILE="${MCPORTER_FIXTURE_FILE:-}" \
      MCPORTER_STDERR_FILE="${MCPORTER_STDERR_FILE:-}" \
      MCPORTER_EXIT_CODE="${MCPORTER_EXIT_CODE:-0}" \
      MCPORTER_CALL_COUNT_FILE="$call_count_file" \
      KENT_TASK_ID= \
      "$@"
  )
}

expect_exit() {
  local expected="$1"
  local actual=0
  shift

  if "$@"; then
    actual=0
  else
    actual=$?
  fi
  if [[ "$actual" -ne "$expected" ]]; then
    printf 'unexpected_exit: expected %s, got %s\n' \
      "$expected" "$actual" >&2
    return 1
  fi
}

run_mobile_tree() {
  run_adapter "$args_log" "$call_adapter" mobile.ui action=tree \
    deviceId=emulator-5554 platform=android "$@"
}

expect_json_unknown() {
  local fixture="$1"
  local pointer="$2"
  local result="$3"

  MCPORTER_FIXTURE_FILE="$fixture" \
    expect_exit 3 run_mobile_tree --assert-json-true "$pointer" \
      >"$result" 2>"$result.err"
  jq -e '
    .schema == "kent-mcp-result-v1" and
    .transport == "succeeded" and
    .processing == "succeeded" and
    .action == "unknown" and
    .assertion == "unknown" and
    .assertionKind == "json_boolean" and
    .assertionResults == ["unknown"]
  ' "$result" >/dev/null
}

expect_json_processing_failure() {
  local fixture="$1"
  local result="$2"
  local pointer="${3-/authenticated}"

  MCPORTER_FIXTURE_FILE="$fixture" \
    expect_exit 3 run_mobile_tree --assert-json-true "$pointer" \
      >"$result" 2>"$result.err"
  jq -e '
    .schema == "kent-mcp-result-v1" and
    .transport == "succeeded" and
    .processing == "failed" and
    .action == "unknown" and
    .assertion == "unknown" and
    .assertionKind == "json_boolean" and
    .assertionResults == ["unknown"]
  ' "$result" >/dev/null
}

make_json_string_fixture() {
  local total_bytes="$1"
  local destination="$2"
  local content_bytes=$((total_bytes - 2))
  local actual_bytes

  {
    printf '"'
    head -c "$content_bytes" /dev/zero | tr '\000' 'a'
    printf '"'
  } >"$destination"
  actual_bytes="$(wc -c <"$destination" | tr -d '[:space:]')"
  [[ "$actual_bytes" == "$total_bytes" ]]
}

write_hash_tokens_fixture() {
  local count="$1"
  local destination="$2"

  : >"$destination"
  for ((token_index = 0; token_index < count; token_index++)); do
    printf 'token%03d\n' "$token_index" >>"$destination"
  done
}

"$call_adapter" --self-test

args_log="$tmp/call-args"
run_adapter "$args_log" "$call_adapter" example.inspect --allow-mutate >/dev/null
grep -Fx -- "--config" "$args_log" >/dev/null
grep -Fx -- "$main/.mcp.json" "$args_log" >/dev/null
grep -Fx -- "--root" "$args_log" >/dev/null
grep -Fx -- "$worktree" "$args_log" >/dev/null
grep -Fx -- "example.inspect" "$args_log" >/dev/null
if find "$worktree/build/mcp-artifacts/_mcp-raw" -type f -print -quit 2>/dev/null |
  grep -q .; then
  echo "MCP call persisted raw output without explicit opt-in" >&2
  exit 1
fi
jq -e '.rawOutputPath == null' \
  "$worktree/build/mcp-artifacts/_mcp-log/mcporter-calls.jsonl" >/dev/null
test ! -e "$worktree/.todo"

# Serena discovery/execution must not inspect or signal unowned processes.
# Exported Bash functions intercept every former sweeper operation.
(
  export PROCESS_PROBE_LOG="$tmp/process-probes"
  export PROCESS_PROBE_ROOT="$worktree"
  pgrep() { echo pgrep >>"$PROCESS_PROBE_LOG"; echo 999999; }
  lsof() { echo lsof >>"$PROCESS_PROBE_LOG"; echo "n$PROCESS_PROBE_ROOT"; }
  ps() { echo ps >>"$PROCESS_PROBE_LOG"; echo 1; }
  kill() { echo "kill $*" >>"$PROCESS_PROBE_LOG"; }
  export -f pgrep lsof ps kill
  run_adapter "$args_log" "$call_adapter" serena.list_dir >/dev/null
  run_adapter "$args_log" "$list_adapter" serena --schema >/dev/null
  test ! -e "$PROCESS_PROBE_LOG"
  test -f "$worktree/.serena/mcp-serena.lock"
  MCPORTER_EXIT_CODE=17 expect_exit 17 \
    run_adapter "$args_log" "$call_adapter" serena.list_dir >/dev/null
  MCPORTER_EXIT_CODE=17 expect_exit 17 \
    run_adapter "$args_log" "$list_adapter" serena >/dev/null
  test ! -e "$PROCESS_PROBE_LOG"
)

KENT_MCP_ARTIFACT_ROOT="$tmp/explicit-artifacts" \
  run_adapter "$args_log" "$call_adapter" example.inspect \
    --allow-mutate --save-raw >/dev/null
test -s "$tmp/explicit-artifacts/_mcp-log/mcporter-calls.jsonl"
test -n "$(find "$tmp/explicit-artifacts/_mcp-raw" -type f -print -quit)"
test ! -e "$worktree/.todo"

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --save-raw >/dev/null
raw_file="$(
  find "$worktree/build/mcp-artifacts/_mcp-raw/example" -type f -print -quit
)"
test -s "$raw_file"
tail -1 "$worktree/build/mcp-artifacts/_mcp-log/mcporter-calls.jsonl" |
  jq -e --arg path "$raw_file" '.rawOutputPath == $path' >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --quiet >"$tmp/quiet.out"
test ! -s "$tmp/quiet.out"
tail -1 "$worktree/build/mcp-artifacts/_mcp-log/mcporter-calls.jsonl" |
  jq -e '.safeOutputMode == "quiet" and .rawOutputPath == null' >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --digest-output >"$tmp/digest.out"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .assertion == "not_requested" and
  .assertionKind == "none" and
  (.sha256 | length) == 64
' \
  "$tmp/digest.out" >/dev/null
if grep -Fq '"ok":true' "$tmp/digest.out"; then
  echo "digest output leaked raw MCP content" >&2
  exit 1
fi

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --assert-contains '"ok":true' \
  --assert-not-contains '"secret"' >"$tmp/assert.out"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .assertion == "passed" and
  .assertionKind == "literal" and
  .contains == 1 and .notContains == 1
' \
  "$tmp/assert.out" >/dev/null
if run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --assert-contains '"missing"' >"$tmp/assert-fail.out"; then
  echo "failed MCP output assertion unexpectedly succeeded" >&2
  exit 1
fi
jq -e '
  .transport == "succeeded" and
  .processing == "succeeded" and
  .assertion == "failed" and
  .assertionKind == "literal" and
  .failedContains == 1
' \
  "$tmp/assert-fail.out" >/dev/null

cat >"$tmp/mobile-auth-true.json" <<'JSON'
{"authenticated":true}
JSON
MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-true.json" \
  run_adapter "$args_log" "$call_adapter" mobile.ui action=tree \
    deviceId=emulator-5554 platform=android \
    --assert-json-true /authenticated >"$tmp/json-true.out"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .action == "unknown" and
  .assertion == "passed" and
  .assertionKind == "json_boolean" and
  .assertionResults == ["passed"]
' "$tmp/json-true.out" >/dev/null

cat >"$tmp/mobile-auth-false.json" <<'JSON'
{"authenticated":false}
JSON
if MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-false.json" \
  run_adapter "$args_log" "$call_adapter" mobile.ui action=tree \
    deviceId=emulator-5554 platform=android \
    --assert-json-true /authenticated >"$tmp/json-false.out"; then
  echo "false JSON boolean assertion unexpectedly succeeded" >&2
  exit 1
else
  json_false_exit=$?
fi
test "$json_false_exit" -eq 3
jq -e '
  .transport == "succeeded" and
  .processing == "succeeded" and
  .assertion == "failed" and
  .assertionKind == "json_boolean" and
  .assertionResults == ["failed"]
' "$tmp/json-false.out" >/dev/null

cat >"$tmp/mobile-auth-missing.json" <<'JSON'
{"other":true}
JSON
if MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-missing.json" \
  run_adapter "$args_log" "$call_adapter" mobile.ui action=tree \
    deviceId=emulator-5554 platform=android \
    --assert-json-true /authenticated >"$tmp/json-missing.out"; then
  echo "missing JSON boolean assertion unexpectedly succeeded" >&2
  exit 1
else
  json_missing_exit=$?
fi
test "$json_missing_exit" -eq 3
jq -e '
  .transport == "succeeded" and
  .processing == "succeeded" and
  .assertion == "unknown" and
  .assertionKind == "json_boolean" and
  .assertionResults == ["unknown"]
' "$tmp/json-missing.out" >/dev/null

cat >"$tmp/json-null.json" <<'JSON'
{"authenticated":null}
JSON
expect_json_unknown "$tmp/json-null.json" /authenticated "$tmp/json-null.out"

cat >"$tmp/json-wrong-type.json" <<'JSON'
{"authenticated":"true"}
JSON
expect_json_unknown "$tmp/json-wrong-type.json" /authenticated \
  "$tmp/json-wrong-type.out"

cat >"$tmp/json-true-and-missing.json" <<'JSON'
{"authenticated":true}
JSON
if MCPORTER_FIXTURE_FILE="$tmp/json-true-and-missing.json" \
  expect_exit 3 run_mobile_tree \
    --assert-json-true /authenticated --assert-json-true /missing \
    >"$tmp/json-true-and-missing.out" \
    2>"$tmp/json-true-and-missing.out.err"; then
  :
else
  echo "mixed true/unknown assertions did not return the expected exit" >&2
  exit 1
fi
jq -e '
  .processing == "succeeded" and
  .assertion == "unknown" and
  .assertionCount == 2 and
  .unknownAssertions == 1 and
  .assertionResults == ["passed","unknown"]
' "$tmp/json-true-and-missing.out" >/dev/null

cat >"$tmp/json-false-and-missing.json" <<'JSON'
{"authenticated":false}
JSON
MCPORTER_FIXTURE_FILE="$tmp/json-false-and-missing.json" \
  expect_exit 3 \
  run_mobile_tree --assert-json-true /authenticated \
    --assert-json-true /missing >"$tmp/json-false-and-missing.out" \
    2>"$tmp/json-false-and-missing.out.err"
jq -e '
  .processing == "succeeded" and
  .assertion == "failed" and
  .assertionCount == 2 and
  .failedAssertions == 1 and
  .unknownAssertions == 1 and
  .assertionResults == ["failed","unknown"]
' "$tmp/json-false-and-missing.out" >/dev/null

cat >"$tmp/json-escaped-pointers.json" <<'JSON'
{"a/b":{"~key":true},"items":[{"ready":true}]}
JSON
MCPORTER_FIXTURE_FILE="$tmp/json-escaped-pointers.json" \
  run_mobile_tree --assert-json-true '/a~1b/~0key' \
    --assert-json-true /items/0/ready >"$tmp/json-escaped-pointers.out"
jq -e '
  .assertion == "passed" and
  .assertionResults == ["passed","passed"]
' "$tmp/json-escaped-pointers.out" >/dev/null

cat >"$tmp/json-array.json" <<'JSON'
{"items":[true]}
JSON
MCPORTER_FIXTURE_FILE="$tmp/json-array.json" \
  expect_exit 3 \
  run_mobile_tree --assert-json-true /items/01 \
    --assert-json-true /items/- --assert-json-true /items/4 \
    >"$tmp/json-array-unknown.out" 2>"$tmp/json-array-unknown.out.err"
jq -e '
  .processing == "succeeded" and
  .assertion == "unknown" and
  .assertionResults == ["unknown","unknown","unknown"]
' "$tmp/json-array-unknown.out" >/dev/null

printf 'true\n' >"$tmp/json-root-true.json"
MCPORTER_FIXTURE_FILE="$tmp/json-root-true.json" \
  run_mobile_tree --assert-json-true "" >"$tmp/json-root-true.out"
jq -e '.assertion == "passed" and .assertionResults == ["passed"]' \
  "$tmp/json-root-true.out" >/dev/null
printf 'false\n' >"$tmp/json-root-false.json"
MCPORTER_FIXTURE_FILE="$tmp/json-root-false.json" \
  expect_exit 3 \
  run_mobile_tree --assert-json-true "" >"$tmp/json-root-false.out" \
    2>"$tmp/json-root-false.out.err"
jq -e '.assertion == "failed" and .assertionResults == ["failed"]' \
  "$tmp/json-root-false.out" >/dev/null

args_hash_before="$(shasum -a 256 "$args_log" | awk '{print $1}')"
expect_exit 2 run_mobile_tree --assert-json-true '/bad~2escape' \
  >"$tmp/json-invalid-pointer.out" 2>"$tmp/json-invalid-pointer.err"
grep -F "safe_output_arguments_invalid" \
  "$tmp/json-invalid-pointer.err" >/dev/null
test ! -s "$tmp/json-invalid-pointer.out"
test "$args_hash_before" = \
  "$(shasum -a 256 "$args_log" | awk '{print $1}')"
expect_exit 2 run_mobile_tree --assert-json-true 'missing-leading-slash' \
  >"$tmp/json-pointer-without-slash.out" \
  2>"$tmp/json-pointer-without-slash.err"
grep -F "safe_output_arguments_invalid" \
  "$tmp/json-pointer-without-slash.err" >/dev/null

cat >"$tmp/json-malformed.json" <<'JSON'
{"authenticated":true
JSON
expect_json_processing_failure "$tmp/json-malformed.json" \
  "$tmp/json-malformed.out"
: >"$tmp/json-empty.json"
expect_json_processing_failure "$tmp/json-empty.json" "$tmp/json-empty.out"
cat >"$tmp/json-duplicate-keys.json" <<'JSON'
{"authenticated":true,"authenticated":false}
JSON
expect_json_processing_failure "$tmp/json-duplicate-keys.json" \
  "$tmp/json-duplicate-keys.out"
printf '\377' >"$tmp/json-invalid-utf8.json"
expect_json_processing_failure "$tmp/json-invalid-utf8.json" \
  "$tmp/json-invalid-utf8.out"
cat >"$tmp/json-nan.json" <<'JSON'
{"authenticated":NaN}
JSON
expect_json_processing_failure "$tmp/json-nan.json" "$tmp/json-nan.out"
cat >"$tmp/json-infinite-float.json" <<'JSON'
{"authenticated":1e999}
JSON
expect_json_processing_failure "$tmp/json-infinite-float.json" \
  "$tmp/json-infinite-float.out"

make_json_string_fixture 1048576 "$tmp/json-exact-limit.json"
test "$(wc -c <"$tmp/json-exact-limit.json" | tr -d '[:space:]')" = 1048576
MCPORTER_FIXTURE_FILE="$tmp/json-exact-limit.json" \
  expect_exit 3 run_mobile_tree --assert-json-true "" \
    >"$tmp/json-exact-limit.out" 2>"$tmp/json-exact-limit.out.err"
jq -e '
  .processing == "succeeded" and
  .assertion == "unknown" and
  .assertionResults == ["unknown"]
' "$tmp/json-exact-limit.out" >/dev/null
MCPORTER_FIXTURE_FILE="$tmp/json-exact-limit.json" \
  run_mobile_tree --assert-contains a >"$tmp/literal-exact-limit.out"
jq -e '.processing == "succeeded" and .assertion == "passed"' \
  "$tmp/literal-exact-limit.out" >/dev/null
MCPORTER_FIXTURE_FILE="$tmp/json-exact-limit.json" \
  run_mobile_tree --hash-matches '^' >"$tmp/extract-exact-limit.out"
jq -e '
  .processing == "succeeded" and
  .occurrenceCount == 1 and
  .uniqueCount == 1
' "$tmp/extract-exact-limit.out" >/dev/null

make_json_string_fixture 1048577 "$tmp/json-over-limit.json"
test "$(wc -c <"$tmp/json-over-limit.json" | tr -d '[:space:]')" = 1048577
expect_json_processing_failure "$tmp/json-over-limit.json" \
  "$tmp/json-over-limit.out" ""
MCPORTER_FIXTURE_FILE="$tmp/json-over-limit.json" \
  expect_exit 3 run_mobile_tree --assert-contains a \
    >"$tmp/literal-over-limit.out" 2>"$tmp/literal-over-limit.out.err"
jq -e '
  .processing == "failed" and
  .assertion == "unknown" and
  .assertionKind == "literal" and
  .assertionResults == ["unknown"]
' "$tmp/literal-over-limit.out" >/dev/null
MCPORTER_FIXTURE_FILE="$tmp/json-over-limit.json" \
  expect_exit 3 run_mobile_tree --hash-matches '^' \
    >"$tmp/extract-over-limit.out" 2>"$tmp/extract-over-limit.out.err"
jq -e '
  .processing == "failed" and
  .assertion == "not_requested" and
  .assertionKind == "none" and
  (has("hashes") | not)
' "$tmp/extract-over-limit.out" >/dev/null
MCPORTER_FIXTURE_FILE="$tmp/json-over-limit.json" \
  run_mobile_tree --digest-output >"$tmp/digest-over-limit.out"
jq -e '
  .processing == "succeeded" and
  .assertion == "not_requested" and
  .bytes == 1048577
' "$tmp/digest-over-limit.out" >/dev/null

printf '\377' >"$tmp/extract-invalid-utf8.json"
MCPORTER_FIXTURE_FILE="$tmp/extract-invalid-utf8.json" \
  expect_exit 3 run_mobile_tree --marker-present marker \
    >"$tmp/extract-invalid-utf8.out" \
    2>"$tmp/extract-invalid-utf8.out.err"
jq -e '
  .processing == "failed" and
  .assertion == "not_requested" and
  .assertionKind == "none"
' "$tmp/extract-invalid-utf8.out" >/dev/null

selector_4096="$(head -c 4096 /dev/zero | tr '\000' 'x')"
selector_4097="${selector_4096}x"
[[ ${#selector_4096} -eq 4096 && ${#selector_4097} -eq 4097 ]]
MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-true.json" \
  run_mobile_tree --assert-not-contains "$selector_4096" \
    >"$tmp/selector-exact-limit.out"
jq -e '.processing == "succeeded" and .assertion == "passed"' \
  "$tmp/selector-exact-limit.out" >/dev/null
expect_exit 2 run_mobile_tree --assert-not-contains "$selector_4097" \
  >"$tmp/selector-over-limit.out" 2>"$tmp/selector-over-limit.err"
grep -F "safe_output_arguments_invalid" \
  "$tmp/selector-over-limit.err" >/dev/null
run_mobile_tree --marker-present "$selector_4096" \
  >"$tmp/marker-exact-limit.out"
jq -e '
  .processing == "succeeded" and
  .markerCount == 1 and
  .markersPresent == [false]
' "$tmp/marker-exact-limit.out" >/dev/null
expect_exit 2 run_mobile_tree --marker-present "$selector_4097" \
  >"$tmp/marker-over-limit.out" 2>"$tmp/marker-over-limit.err"
grep -F "safe_output_arguments_invalid" \
  "$tmp/marker-over-limit.err" >/dev/null

pointer_4096="/$(head -c 4095 /dev/zero | tr '\000' 'x')"
pointer_4097="${pointer_4096}x"
[[ ${#pointer_4096} -eq 4096 && ${#pointer_4097} -eq 4097 ]]
MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-true.json" \
  expect_exit 3 run_mobile_tree --assert-json-true "$pointer_4096" \
    >"$tmp/pointer-exact-limit.out" \
    2>"$tmp/pointer-exact-limit.out.err"
jq -e '.processing == "succeeded" and .assertion == "unknown"' \
  "$tmp/pointer-exact-limit.out" >/dev/null
expect_exit 2 run_mobile_tree --assert-json-true "$pointer_4097" \
  >"$tmp/pointer-over-limit.out" 2>"$tmp/pointer-over-limit.err"
grep -F "safe_output_arguments_invalid" \
  "$tmp/pointer-over-limit.err" >/dev/null

regex_4096="$(head -c 4096 /dev/zero | tr '\000' 'a')"
regex_4097="${regex_4096}a"
MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-true.json" \
  run_mobile_tree --hash-matches "$regex_4096" \
    >"$tmp/regex-exact-limit.out"
jq -e '
  .processing == "succeeded" and
  .occurrenceCount == 0 and
  .uniqueCount == 0
' "$tmp/regex-exact-limit.out" >/dev/null
expect_exit 2 run_mobile_tree --hash-matches "$regex_4097" \
  >"$tmp/regex-over-limit.out" 2>"$tmp/regex-over-limit.err"
grep -F "safe_output_arguments_invalid" \
  "$tmp/regex-over-limit.err" >/dev/null

predicates_32=()
for ((selector_index = 0; selector_index < 32; selector_index++)); do
  predicates_32+=(--assert-json-true /authenticated)
done
MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-true.json" \
  run_mobile_tree "${predicates_32[@]}" >"$tmp/predicates-32.out"
jq -e '
  .assertion == "passed" and
  .assertionCount == 32 and
  (.assertionResults | length) == 32 and
  ([.assertionResults[] | select(. != "passed")] | length) == 0
' "$tmp/predicates-32.out" >/dev/null
predicates_33=("${predicates_32[@]}" --assert-json-true /authenticated)
args_hash_before="$(shasum -a 256 "$args_log" | awk '{print $1}')"
expect_exit 2 run_mobile_tree "${predicates_33[@]}" \
  >"$tmp/predicates-33.out" 2>"$tmp/predicates-33.err"
grep -F "safe_output_arguments_invalid" "$tmp/predicates-33.err" >/dev/null
test "$args_hash_before" = \
  "$(shasum -a 256 "$args_log" | awk '{print $1}')"

markers_32=()
for ((selector_index = 0; selector_index < 32; selector_index++)); do
  markers_32+=(--marker-present ok)
done
run_mobile_tree "${markers_32[@]}" >"$tmp/markers-32.out"
jq -e '
  .processing == "succeeded" and
  .assertion == "not_requested" and
  .markerCount == 32 and
  (.markersPresent | length) == 32 and
  ([.markersPresent[] | select(. != true)] | length) == 0
' "$tmp/markers-32.out" >/dev/null
markers_33=("${markers_32[@]}" --marker-present ok)
expect_exit 2 run_mobile_tree "${markers_33[@]}" \
  >"$tmp/markers-33.out" 2>"$tmp/markers-33.err"
grep -F "safe_output_arguments_invalid" "$tmp/markers-33.err" >/dev/null

write_hash_tokens_fixture 256 "$tmp/hash-256.txt"
MCPORTER_FIXTURE_FILE="$tmp/hash-256.txt" \
  run_mobile_tree --hash-matches 'token[0-9]{3}' \
    >"$tmp/hash-256.out"
jq -e '
  .processing == "succeeded" and
  .occurrenceCount == 256 and
  .uniqueCount == 256 and
  (.hashes | length) == 256
' "$tmp/hash-256.out" >/dev/null
write_hash_tokens_fixture 257 "$tmp/hash-257.txt"
MCPORTER_FIXTURE_FILE="$tmp/hash-257.txt" \
  expect_exit 3 run_mobile_tree --hash-matches 'token[0-9]{3}' \
    >"$tmp/hash-257.out" 2>"$tmp/hash-257.out.err"
jq -e '
  .processing == "failed" and
  .assertion == "not_requested" and
  (has("hashes") | not)
' "$tmp/hash-257.out" >/dev/null

run_mobile_tree --hash-matches 'ok' --marker-present '"ok":true' \
  >"$tmp/extract-all-true.out"
jq -e '
  .processing == "succeeded" and
  .assertion == "not_requested" and
  .markersPresent == [true]
' "$tmp/extract-all-true.out" >/dev/null
MCPORTER_FIXTURE_FILE="$tmp/mobile-auth-true.json" \
  expect_exit 3 run_mobile_tree --hash-matches '[' \
    >"$tmp/extract-invalid-regex.out" \
    2>"$tmp/extract-invalid-regex.out.err"
jq -e '
  .processing == "failed" and
  .assertion == "not_requested" and
  .assertionKind == "none"
' "$tmp/extract-invalid-regex.out" >/dev/null

cat >"$tmp/mobile-transport-response.json" <<'JSON'
{"authenticated":true,"private":"SENTINEL_TRANSPORT_RESPONSE"}
JSON
printf '%s\n' 'SENTINEL_TRANSPORT_STDERR' >"$tmp/mobile-transport-stderr.txt"
MCPORTER_FIXTURE_FILE="$tmp/mobile-transport-response.json" \
MCPORTER_STDERR_FILE="$tmp/mobile-transport-stderr.txt" \
MCPORTER_EXIT_CODE=37 \
  expect_exit 37 run_mobile_tree --assert-json-true /authenticated \
    >"$tmp/mobile-transport-failure.out" \
    2>"$tmp/mobile-transport-failure.err"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "failed" and
  .processing == "not_attempted" and
  .action == "unknown" and
  .assertion == "unknown" and
  .assertionKind == "json_boolean" and
  .assertionResults == ["unknown"]
' "$tmp/mobile-transport-failure.out" >/dev/null
tail -1 "$worktree/build/mcp-artifacts/_mcp-log/mcporter-calls.jsonl" |
  jq -e '
    .exitCode == 37 and
    .errorCode == "mcporter_call_failed" and
    .safeOutputMode == "json_assertion" and
    .rawOutputPath == null
  ' >/dev/null
if grep -Fq 'SENTINEL_TRANSPORT' \
  "$tmp/mobile-transport-failure.out" "$tmp/mobile-transport-failure.err"; then
  echo "safe transport failure exposed raw output or provider stderr" >&2
  exit 1
fi

MCPORTER_STDERR_FILE="$tmp/mobile-transport-stderr.txt" \
MCPORTER_EXIT_CODE=37 \
  expect_exit 37 run_mobile_tree --digest-output \
    >"$tmp/mobile-transport-digest.out" \
    2>"$tmp/mobile-transport-digest.err"
jq -e '
  .transport == "failed" and
  .processing == "not_attempted" and
  .assertion == "not_requested" and
  .assertionKind == "none"
' "$tmp/mobile-transport-digest.out" >/dev/null
MCPORTER_STDERR_FILE="$tmp/mobile-transport-stderr.txt" \
MCPORTER_EXIT_CODE=37 \
  expect_exit 37 run_mobile_tree --quiet \
    >"$tmp/mobile-transport-quiet.out" \
    2>"$tmp/mobile-transport-quiet.err"
test ! -s "$tmp/mobile-transport-quiet.out"
if grep -Fq 'SENTINEL_TRANSPORT' "$tmp/mobile-transport-quiet.err"; then
  echo "quiet transport failure exposed provider stderr" >&2
  exit 1
fi

cat >"$tmp/mobile-secret-response.json" <<'JSON'
{"SENTINEL_SELECTOR_SECRET":true,"private":"SENTINEL_SELECTED_VALUE_SECRET","body":"SENTINEL_RESPONSE_BODY_SECRET"}
JSON
printf '%s\n' 'SENTINEL_PROVIDER_STDERR_SECRET' \
  >"$tmp/mobile-secret-stderr.txt"
MCPORTER_FIXTURE_FILE="$tmp/mobile-secret-response.json" \
MCPORTER_STDERR_FILE="$tmp/mobile-secret-stderr.txt" \
  run_mobile_tree --assert-json-true /SENTINEL_SELECTOR_SECRET \
    >"$tmp/mobile-secret-result.out" \
    2>"$tmp/mobile-secret-result.err"
jq -e '.assertion == "passed" and .assertionResults == ["passed"]' \
  "$tmp/mobile-secret-result.out" >/dev/null
MCPORTER_FIXTURE_FILE="$tmp/mobile-secret-response.json" \
MCPORTER_STDERR_FILE="$tmp/mobile-secret-stderr.txt" \
  expect_exit 3 run_mobile_tree --assert-json-true /private \
    >"$tmp/mobile-selected-value-result.out" \
    2>"$tmp/mobile-selected-value-result.err"
jq -e '.assertion == "unknown" and .assertionResults == ["unknown"]' \
  "$tmp/mobile-selected-value-result.out" >/dev/null
for safe_artifact in \
  "$tmp/mobile-secret-result.out" \
  "$tmp/mobile-secret-result.err" \
  "$tmp/mobile-selected-value-result.out" \
  "$tmp/mobile-selected-value-result.err" \
  "$tmp/mobile-transport-failure.out" \
  "$tmp/mobile-transport-failure.err" \
  "$worktree/build/mcp-artifacts/_mcp-log/mcporter-calls.jsonl" \
  "$args_log"; do
  if grep -E -q 'SENTINEL_(SELECTOR|SELECTED|RESPONSE|PROVIDER|TRANSPORT)' \
    "$safe_artifact"; then
    echo "safe mobile output persisted or emitted a sentinel" >&2
    exit 1
  fi
done
if grep -Fq '/SENTINEL_SELECTOR_SECRET' "$args_log"; then
  echo "JSON pointer was forwarded to mcporter arguments" >&2
  exit 1
fi
if find "$worktree/build/mcp-artifacts/_mcp-raw/mobile" -type f -print -quit \
  2>/dev/null | grep -q .; then
  echo "safe mobile output persisted a raw artifact" >&2
  exit 1
fi
tail -1 "$worktree/build/mcp-artifacts/_mcp-log/mcporter-calls.jsonl" |
  jq -e '.rawOutputPath == null' >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --hash-matches 'ok|missing' \
  --marker-present '"ok":true' \
  --marker-present '"secret"' >"$tmp/extract.out"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .action == "unknown" and
  .assertion == "not_requested" and
  .assertionKind == "none" and
  .occurrenceCount == 1 and
  .uniqueCount == 1 and
  (.hashes | length) == 1 and
  .markerCount == 2 and
  .markersPresent == [true, false]
' "$tmp/extract.out" >/dev/null
if grep -Fq 'ok' "$tmp/extract.out"; then
  echo "hash extraction leaked matched MCP content" >&2
  exit 1
fi

if run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --quiet --save-raw \
  >"$tmp/safe-raw.out" 2>"$tmp/safe-raw.err"; then
  echo "safe output mode unexpectedly saved raw output" >&2
  exit 1
fi
grep -F "safe_output_cannot_save_raw" "$tmp/safe-raw.err" >/dev/null

project_config="$tmp/project.json"
echo '{"mcpServers":{}}' >"$project_config"
echo "MCP_CONFIG_PATH=$project_config" >"$home/.kent/mcp.Example.env"
run_adapter "$args_log" "$call_adapter" example.inspect --allow-mutate --no-save-raw >/dev/null
grep -Fx -- "$project_config" "$args_log" >/dev/null

if (
  cd "$worktree"
  HOME="$home" \
    PATH="$fake_bin:$PATH" \
    MCPORTER_ARGS_LOG="$args_log" \
    MCP_CONFIG_PATH=relative.json \
    "$call_adapter" example.inspect --allow-mutate --no-save-raw
) >"$tmp/relative.out" 2>"$tmp/relative.err"; then
  echo "relative MCP_CONFIG_PATH unexpectedly succeeded" >&2
  exit 1
fi
grep -F "MCP_CONFIG_PATH must be absolute" "$tmp/relative.err" >/dev/null

if run_adapter "$args_log" \
  "$call_adapter" mobile.input action=tap deviceId=emulator-5554 \
  platform=android --quiet >"$tmp/mutate.out" 2>"$tmp/mutate.err"; then
  echo "mutating mobile call unexpectedly succeeded without approval" >&2
  exit 1
fi
grep -F "requires --allow-mutate" "$tmp/mutate.err" >/dev/null

if run_adapter "$args_log" \
  "$call_adapter" mobile.input action=tap platform=android \
  --allow-mutate --quiet >"$tmp/missing-device.out" \
  2>"$tmp/missing-device.err"; then
  echo "mobile call without deviceId unexpectedly succeeded" >&2
  exit 1
fi
grep -F "mobile_exact_target_required" "$tmp/missing-device.err" >/dev/null

if run_adapter "$args_log" \
  "$call_adapter" mobile.ui action=tree deviceId=emulator-5554 \
  --digest-output >"$tmp/missing-platform.out" \
  2>"$tmp/missing-platform.err"; then
  echo "mobile call without platform unexpectedly succeeded" >&2
  exit 1
fi
grep -F "mobile_exact_target_required" "$tmp/missing-platform.err" >/dev/null

if run_adapter "$args_log" \
  "$call_adapter" mobile.system action=clipboard_paste platform=android \
  --allow-mutate --quiet >"$tmp/implicit-system.out" \
  2>"$tmp/implicit-system.err"; then
  echo "implicit-target mobile system call unexpectedly succeeded" >&2
  exit 1
fi
grep -F "unsupported_mobile_implicit_target" \
  "$tmp/implicit-system.err" >/dev/null

if run_adapter "$args_log" \
  "$call_adapter" mobile.ui action=tree deviceId=emulator-5554 \
  platform=android --no-save-raw \
  >"$tmp/mobile-raw.out" 2>"$tmp/mobile-raw.err"; then
  echo "sensitive mobile call unexpectedly emitted raw output" >&2
  exit 1
fi
grep -F "sensitive_mobile_output_requires_safe_mode" \
  "$tmp/mobile-raw.err" >/dev/null

run_adapter "$args_log" \
  "$call_adapter" mobile.input action=tap deviceId=emulator-5554 \
  platform=android --allow-mutate --quiet >"$tmp/mobile-quiet.out"
test ! -s "$tmp/mobile-quiet.out"
grep -Fx -- "$home/.mcporter/mcporter.json" "$args_log" >/dev/null
if grep -Fx -- "$project_config" "$args_log" >/dev/null; then
  echo "global mobile unexpectedly used project MCP_CONFIG_PATH" >&2
  exit 1
fi

run_adapter "$args_log" \
  "$call_adapter" mobile.ui action=tree deviceId=emulator-5554 \
  platform=android --digest-output >"$tmp/mobile-digest.out"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .assertion == "not_requested" and
  .assertionKind == "none" and
  (.sha256 | length) == 64
' \
  "$tmp/mobile-digest.out" >/dev/null
if grep -Fq '"ok":true' "$tmp/mobile-digest.out"; then
  echo "mobile digest output leaked raw MCP content" >&2
  exit 1
fi

run_adapter "$args_log" \
  "$call_adapter" mobile.ui action=tree deviceId=emulator-5554 \
  platform=android --hash-matches 'ok' \
  --marker-present 'history_final_page' >"$tmp/mobile-extract.out"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .action == "unknown" and
  .assertion == "not_requested" and
  .assertionKind == "none" and
  .uniqueCount == 1 and
  .markersPresent == [false]
' "$tmp/mobile-extract.out" >/dev/null

if run_adapter "$args_log" \
  "$call_adapter" mobile.device action=get_target --no-save-raw \
  >"$tmp/state.out" 2>"$tmp/state.err"; then
  echo "stateful default mobile targeting unexpectedly succeeded" >&2
  exit 1
fi
grep -F "default mobile is stateless" "$tmp/state.err" >/dev/null

mkdir -p "$main/.kent/adapters/mcp/servers"
cat >"$main/.kent/adapters/mcp/servers/custom" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$main/.kent/adapters/mcp/servers/custom"
cat >"$main/.kent/adapters/mcp/policy" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == "custom.inspect" ]]; then
  echo read-only
else
  echo inherit
fi
SH
chmod +x "$main/.kent/adapters/mcp/policy"
run_adapter "$args_log" "$call_adapter" custom.inspect --no-save-raw >/dev/null
grep -Fx -- "--stdio" "$args_log" >/dev/null
grep -Fx -- "$main/.kent/adapters/mcp/servers/custom" "$args_log" >/dev/null

rm -f "$home/.kent/mcp.Example.env"
run_adapter "$args_log" "$list_adapter" example --schema >/dev/null
test -s "$worktree/build/mcp-cache/example-schema.json"
grep -Fx -- "--json" "$args_log" >/dev/null

# Processing success does not imply that an expected destination is present.
cat >"$tmp/mobile-negative.json" <<'JSON'
{"destination":"phone_login","error":false}
JSON
MCPORTER_FIXTURE_FILE="$tmp/mobile-negative.json" \
  run_adapter "$args_log" "$call_adapter" mobile.ui action=tree \
    deviceId=emulator-5554 platform=android \
    --hash-matches 'phone_login|home' --marker-present '"home"' \
    >"$tmp/mobile-negative-result.json"
jq -e '
  .schema == "kent-mcp-result-v1" and
  .transport == "succeeded" and
  .processing == "succeeded" and
  .action == "unknown" and
  .assertion == "not_requested" and
  .assertionKind == "none" and
  .markersPresent == [false]
' "$tmp/mobile-negative-result.json" >/dev/null

# Literal assertions do not interpret provider errors or request echoes.
cat >"$tmp/request-echo-in-error.json" <<'JSON'
{"error":"failed","request":{"authenticated":true}}
JSON
MCPORTER_FIXTURE_FILE="$tmp/request-echo-in-error.json" \
  run_mobile_tree --assert-json-true /request/authenticated \
    >"$tmp/request-echo-in-error.out"
jq -e '.assertion == "passed" and .assertionResults == ["passed"]' \
  "$tmp/request-echo-in-error.out" >/dev/null
jq -e '.action == "unknown"' "$tmp/request-echo-in-error.out" >/dev/null

cat >"$tmp/positive-with-contradictory-error.json" <<'JSON'
{"authenticated":true,"error":true}
JSON
MCPORTER_FIXTURE_FILE="$tmp/positive-with-contradictory-error.json" \
  run_mobile_tree --assert-json-true /authenticated \
    >"$tmp/positive-with-contradictory-error.out"
jq -e '.assertion == "passed" and .assertionResults == ["passed"]' \
  "$tmp/positive-with-contradictory-error.out" >/dev/null
jq -e '.action == "unknown"' "$tmp/positive-with-contradictory-error.out" >/dev/null

echo r2 >"$home/.kent/mcp-mode"
# Caller instructions and graph artifacts are not storage authority. The real
# installed-core/receipt boundary is exercised by test_project_memory.py; this
# wire fixture ensures wrappers do not add their own source-admission gate.
mkdir -p "$r2_worktree/.kent/workflows" "$r2_worktree/.kent/scripts"
echo '# unrelated caller profile update' >"$r2_worktree/.kent/workflow-profile.toml"
echo '{"unpublished_graph_artifact":true}' >"$r2_worktree/.kent/workflows/candidate.json"
cat >"$r2_worktree/.kent/scripts/inspect-delivery-source" <<'SH'
#!/usr/bin/env bash
echo "unexpected caller source admission" >&2
exit 97
SH
chmod +x "$r2_worktree/.kent/scripts/inspect-delivery-source"
test ! -e "$r2_worktree/.todo"
test ! -e "$r2_worktree/build/mcp-cache"
r2_trace_start="$(wc -l <"$home/.kent/mcp-protocol.jsonl" | tr -d '[:space:]')"
r2_calls_start="$(wc -l <"$call_count_file" | tr -d '[:space:]')"

if run_r2_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --standalone-binding "$standalone_binding" --quiet \
  >"$tmp/r2-no-invocation.out" 2>"$tmp/r2-no-invocation.err"; then
  echo "R2 MCP call without a preknown invocation ID unexpectedly succeeded" >&2
  exit 1
fi
if ! grep -E 'invocation-id|mcp_' "$tmp/r2-no-invocation.err" >/dev/null; then
  echo "R2 call without invocation ID failed for an unexpected reason:" >&2
  sed -n '1,12p' "$tmp/r2-no-invocation.err" >&2
  exit 1
fi
test ! -s "$tmp/r2-no-invocation.out"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" = "$r2_calls_start"
no_token_requests="$tmp/r2-no-token-requests.jsonl"
sed -n "$((r2_trace_start + 1)),$ p" "$home/.kent/mcp-protocol.jsonl" \
  >"$no_token_requests"
jq -se 'length == 1 and .[0].operation == "resolve-mcp"' \
  "$no_token_requests" >/dev/null

list_trace_start="$(wc -l <"$home/.kent/mcp-protocol.jsonl" | tr -d '[:space:]')"
if run_r2_adapter "$args_log" "$list_adapter" example --schema \
  --standalone-binding "$standalone_binding" \
  >"$tmp/r2-list-no-invocation.out" 2>"$tmp/r2-list-no-invocation.err"; then
  echo "R2 MCP list without a preknown invocation ID unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'mcp_invocation_id_required' "$tmp/r2-list-no-invocation.err" >/dev/null
test ! -s "$tmp/r2-list-no-invocation.out"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" = "$r2_calls_start"
list_no_token_requests="$tmp/r2-list-no-token-requests.jsonl"
sed -n "$((list_trace_start + 1)),$ p" "$home/.kent/mcp-protocol.jsonl" \
  >"$list_no_token_requests"
jq -se 'length == 1 and .[0].operation == "resolve-mcp"' \
  "$list_no_token_requests" >/dev/null

cat >"$tmp/r2-response.json" <<'JSON'
{"result":"response-private"}
JSON
quiet_invocation="$(new_r2_invocation)"
quiet_id="$(jq -er '.result.invocation_id' <<<"$quiet_invocation")"
quiet_started="$(jq -er '.result.started_path' <<<"$quiet_invocation")"
quiet_finished="$(jq -er '.result.finished_path' <<<"$quiet_invocation")"
test ! -e "$quiet_started"
test ! -e "$quiet_finished"
MCPORTER_FIXTURE_FILE="$tmp/r2-response.json" \
KENT_MCP_ARTIFACT_ROOT="$tmp/outside-artifacts" \
  run_r2_adapter "$args_log" "$call_adapter" example.inspect \
    credential=credential-private --allow-mutate --quiet \
    --standalone-binding "$standalone_binding" \
    --invocation-id "$quiet_id" >"$tmp/r2-quiet.out"
test ! -s "$tmp/r2-quiet.out"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((r2_calls_start + 1))"
test -f "$quiet_started"
test -f "$quiet_finished"
jq -e '
  .permitted_metadata.operation == "call" and
  .permitted_metadata.server == "example" and
  .permitted_metadata.tool == "inspect" and
  .permitted_metadata.output_mode == "text" and
  .permitted_metadata.safe_mode == true and
  .permitted_metadata.save_raw == false
' "$quiet_finished" >/dev/null
test ! -e "$tmp/outside-artifacts"
test ! -e "$r2_worktree/.todo"
test ! -e "$r2_worktree/build/mcp-cache"
if grep -Fq -- "--invocation-id" "$args_log" ||
  grep -Fq -- "--standalone-binding" "$args_log"; then
  echo "R2 adapter options leaked through to mcporter" >&2
  exit 1
fi
if grep -R -E 'credential-private|response-private' \
  "$r2_project_path/tasks" "$home/.kent/mcp-protocol.jsonl"; then
  echo "suppressed MCP input/output leaked into R2 metadata or helper logs" >&2
  exit 1
fi

if run_r2_adapter "$args_log" "$call_adapter" example.inspect \
  credential=credential-private --allow-mutate --quiet \
  --standalone-binding "$standalone_binding" \
  --invocation-id "$quiet_id" >"$tmp/r2-duplicate.out" \
  2>"$tmp/r2-duplicate.err"; then
  echo "finished R2 invocation replay unexpectedly succeeded" >&2
  exit 1
fi
test ! -s "$tmp/r2-duplicate.out"
grep -E 'finished.*read-invocation|read-invocation.*finished' \
  "$tmp/r2-duplicate.err" >/dev/null
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((r2_calls_start + 1))"

if run_r2_adapter "$args_log" "$call_adapter" example.inspect \
  credential=changed-private --allow-mutate --quiet \
  --standalone-binding "$standalone_binding" \
  --invocation-id "$quiet_id" >"$tmp/r2-conflict.out" \
  2>"$tmp/r2-conflict.err"; then
  echo "R2 invocation token with a changed request unexpectedly succeeded" >&2
  exit 1
fi
test ! -s "$tmp/r2-conflict.out"
grep -Ei 'conflict|request differs' "$tmp/r2-conflict.err" >/dev/null
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((r2_calls_start + 1))"

unknown_invocation="$(new_r2_invocation)"
unknown_id="$(jq -er '.result.invocation_id' <<<"$unknown_invocation")"
printf '%s\n' "$unknown_id" >"$home/.kent/force-unknown-id"
if run_r2_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --quiet --standalone-binding "$standalone_binding" \
  --invocation-id "$unknown_id" >"$tmp/r2-unknown.out" \
  2>"$tmp/r2-unknown.err"; then
  echo "unresolved started R2 invocation unexpectedly replayed" >&2
  exit 1
fi
test ! -s "$tmp/r2-unknown.out"
grep -E 'unknown.*read-invocation|read-invocation.*unknown' \
  "$tmp/r2-unknown.err" >/dev/null
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((r2_calls_start + 1))"
rm "$home/.kent/force-unknown-id"

raw_invocation="$(new_r2_invocation)"
raw_id="$(jq -er '.result.invocation_id' <<<"$raw_invocation")"
raw_started="$(jq -er '.result.started_path' <<<"$raw_invocation")"
if run_r2_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --raw-dir "$tmp/outside-raw" \
  --standalone-binding "$standalone_binding" \
  --invocation-id "$raw_id" >"$tmp/r2-raw-dir.out" \
  2>"$tmp/r2-raw-dir.err"; then
  echo "R2 MCP call accepted an escaping --raw-dir" >&2
  exit 1
fi
test ! -e "$tmp/outside-raw"
test ! -e "$raw_started"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((r2_calls_start + 1))"

saved_invocation="$(new_r2_invocation)"
saved_id="$(jq -er '.result.invocation_id' <<<"$saved_invocation")"
saved_finished="$(jq -er '.result.finished_path' <<<"$saved_invocation")"
MCPORTER_FIXTURE_FILE="$tmp/r2-response.json" \
KENT_MCP_ARTIFACT_ROOT="$tmp/outside-artifacts" \
  run_r2_adapter "$args_log" "$call_adapter" example.inspect \
    --allow-mutate --save-raw \
    --standalone-binding "$standalone_binding" \
    --invocation-id "$saved_id" >"$tmp/r2-save-raw.out"
cmp -s "$tmp/r2-response.json" "$tmp/r2-save-raw.out"
saved_sha="$(shasum -a 256 "$tmp/r2-response.json" | awk '{print $1}')"
saved_object="$r2_project_path/tasks/intake-22222222-2222-4222-8222-222222222222/artifacts/mcp/objects/$saved_sha"
test -s "$saved_object"
cmp -s "$tmp/r2-response.json" "$saved_object"
jq -e --arg digest "$saved_sha" '.object_sha256 == $digest' \
  "$saved_finished" >/dev/null
jq -e '
  .permitted_metadata.safe_mode == false and
  .permitted_metadata.save_raw == true
' "$saved_finished" >/dev/null
test ! -e "$tmp/outside-artifacts"
test ! -e "$r2_worktree/.todo"

same_request_one="$(new_r2_invocation)"
same_request_two="$(new_r2_invocation)"
same_id_one="$(jq -er '.result.invocation_id' <<<"$same_request_one")"
same_id_two="$(jq -er '.result.invocation_id' <<<"$same_request_two")"
same_finished_one="$(jq -er '.result.finished_path' <<<"$same_request_one")"
same_finished_two="$(jq -er '.result.finished_path' <<<"$same_request_two")"
MCPORTER_FIXTURE_FILE="$tmp/r2-response.json" \
  run_r2_adapter "$args_log" "$call_adapter" example.inspect stable=yes \
    --allow-mutate --quiet --standalone-binding "$standalone_binding" \
    --invocation-id "$same_id_one" >"$tmp/r2-same-one.out"
MCPORTER_FIXTURE_FILE="$tmp/r2-response.json" \
  run_r2_adapter "$args_log" "$call_adapter" example.inspect stable=yes \
    --allow-mutate --quiet --standalone-binding "$standalone_binding" \
    --invocation-id "$same_id_two" >"$tmp/r2-same-two.out"
test "$same_id_one" != "$same_id_two"
test -f "$same_finished_one"
test -f "$same_finished_two"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((r2_calls_start + 4))"

cat >"$tmp/r2-schema.json" <<'JSON'
{"tools":[{"name":"inspect"}]}
JSON
schema_invocation="$(new_r2_invocation)"
schema_id="$(jq -er '.result.invocation_id' <<<"$schema_invocation")"
schema_finished="$(jq -er '.result.finished_path' <<<"$schema_invocation")"
schema_calls_before="$(wc -l <"$call_count_file" | tr -d '[:space:]')"
MCPORTER_FIXTURE_FILE="$tmp/r2-schema.json" \
  run_r2_adapter "$args_log" "$list_adapter" example --schema \
    --standalone-binding "$standalone_binding" --invocation-id "$schema_id" \
    >"$tmp/r2-schema-first.out"
cmp -s "$tmp/r2-schema.json" "$tmp/r2-schema-first.out"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((schema_calls_before + 1))"
jq -e '.object_sha256 != null' "$schema_finished" >/dev/null
jq -e '
  .permitted_metadata.operation == "list" and
  .permitted_metadata.server == "example" and
  .permitted_metadata.tool == "list-schema" and
  .permitted_metadata.output_mode == "json" and
  .permitted_metadata.safe_mode == false and
  .permitted_metadata.save_raw == false
' "$schema_finished" >/dev/null
test ! -e "$r2_worktree/build/mcp-cache"

cached_schema_invocation="$(new_r2_invocation)"
cached_schema_id="$(jq -er '.result.invocation_id' \
  <<<"$cached_schema_invocation")"
cached_schema_finished="$(jq -er '.result.finished_path' \
  <<<"$cached_schema_invocation")"
MCPORTER_FIXTURE_FILE="$tmp/another-schema.json" \
  run_r2_adapter "$args_log" "$list_adapter" example --schema \
    --standalone-binding "$standalone_binding" \
    --invocation-id "$cached_schema_id" \
    >"$tmp/r2-schema-cache-hit.out"
cmp -s "$tmp/r2-schema.json" "$tmp/r2-schema-cache-hit.out"
test "$(wc -l <"$call_count_file" | tr -d '[:space:]')" \
  = "$((schema_calls_before + 1))"
jq -e '.object_sha256 != null' "$cached_schema_finished" >/dev/null
test ! -e "$r2_worktree/.todo"
test ! -e "$r2_worktree/build/mcp-cache"

r2_requests="$tmp/r2-requests.jsonl"
sed -n "$((r2_trace_start + 1)),$ p" "$home/.kent/mcp-protocol.jsonl" \
  >"$r2_requests"
jq -se --arg workspace "$r2_worktree" '
  all(.[]; .workspace_path == $workspace) and
  all(.[]; (.credential // "") == "" and (.arguments // "") == "") and
  all(.[]; has("owner_token") | not)
' "$r2_requests" >/dev/null
for invocation_id in "$quiet_id" "$saved_id" "$schema_id" "$cached_schema_id"; do
  if ! jq -se --arg id "$invocation_id" '
    [.[] | select(.invocation_id == $id) | .operation] as $ops |
    ($ops[0] == "begin-invocation") and
    ("finish-invocation" as $finish | ($ops | index($finish)) != null)
  ' "$r2_requests" >/dev/null; then
    echo "missing begin/finish invocation sequence for $invocation_id:" >&2
    jq -cs --arg id "$invocation_id" \
      '[.[] | select(.invocation_id == $id) | .operation]' "$r2_requests" >&2
    exit 1
  fi
done
jq -se --arg id "$cached_schema_id" '
  to_entries as $entries |
  ($entries | map(select(.value.invocation_id == $id and
    .value.operation == "begin-invocation")) | .[0].key) as $begin |
  ($entries | map(select(.value.invocation_id == $id and
    .value.operation == "finish-invocation")) | .[0].key) as $finish |
  any($entries[]; .key > $begin and .key < $finish and
    .value.operation == "read-cache")
' "$r2_requests" >/dev/null

echo "MCP adapter tests passed"
