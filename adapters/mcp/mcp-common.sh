#!/usr/bin/env bash
set -euo pipefail

workspace_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

primary_workspace_root() {
  local root="$1"
  local primary

  primary="$(
    git -C "$root" worktree list --porcelain 2>/dev/null |
      sed -n 's/^worktree //p' |
      head -n 1
  )"
  if [[ -n "$primary" && -d "$primary" ]]; then
    printf '%s\n' "$primary"
  else
    printf '%s\n' "$root"
  fi
}

read_env_file_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      ""|\#*) continue ;;
    esac
    [[ "$line" == "$key="* ]] || continue
    printf '%s\n' "${line#"$key="}"
  done <"$file" | tail -n 1
}

resolve_mcp_config() {
  local root primary root_name project_name
  local global_value project_value workspace_value process_value config_path

  root="$(workspace_root)"
  primary="$(primary_workspace_root "$root")"
  root_name="$(basename "$root")"
  project_name="$(basename "$primary")"

  global_value="$(read_env_file_value "$HOME/.kent/mcp.env" MCP_CONFIG_PATH || true)"
  project_value="$(
    read_env_file_value "$HOME/.kent/mcp.${project_name}.env" MCP_CONFIG_PATH ||
      true
  )"
  workspace_value=""
  if [[ "$root_name" != "$project_name" ]]; then
    workspace_value="$(
      read_env_file_value "$HOME/.kent/mcp.${root_name}.env" MCP_CONFIG_PATH ||
        true
    )"
  fi
  process_value="${MCP_CONFIG_PATH-}"

  config_path="$global_value"
  [[ -n "$project_value" ]] && config_path="$project_value"
  [[ -n "$workspace_value" ]] && config_path="$workspace_value"
  [[ -n "$process_value" ]] && config_path="$process_value"

  if [[ -n "$config_path" ]]; then
    if [[ "$config_path" != /* ]]; then
      printf 'mcp_config_relative: MCP_CONFIG_PATH must be absolute: %s\n' \
        "$config_path" >&2
      return 2
    fi
    if [[ ! -f "$config_path" ]]; then
      printf 'mcp_config_missing: MCP_CONFIG_PATH does not point to a readable file: %s\n' \
        "$config_path" >&2
      return 2
    fi
    printf '%s\n' "$config_path"
    return 0
  fi

  if [[ -f "$root/.mcp.json" ]]; then
    printf '%s\n' "$root/.mcp.json"
    return 0
  fi
  if [[ "$primary" != "$root" && -f "$primary/.mcp.json" ]]; then
    printf '%s\n' "$primary/.mcp.json"
    return 0
  fi

  printf '\n'
}

require_mcporter() {
  if ! command -v mcporter >/dev/null 2>&1; then
    printf 'mcporter_missing: install mcporter and ensure it is available in PATH\n' >&2
    return 127
  fi
}

mcporter_home_config_path() {
  local explicit base legacy candidate

  explicit="${MCPORTER_CONFIG-}"
  if [[ -n "$explicit" ]]; then
    python3 - "$explicit" <<'PY'
import os
import sys

print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
    return
  fi

  legacy="$HOME/.mcporter"
  base="$legacy"
  if [[ -n "${XDG_CONFIG_HOME:-}" && "${XDG_CONFIG_HOME:-}" == /* ]]; then
    base="$XDG_CONFIG_HOME/mcporter"
  fi

  for candidate in \
    "$base/mcporter.json" \
    "$base/mcporter.jsonc" \
    "$legacy/mcporter.json" \
    "$legacy/mcporter.jsonc"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  printf '%s\n' "$base/mcporter.json"
}

timestamp_utc() {
  date -u +%Y%m%d-%H%M%S
}

safe_name() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '-'
}

output_ext() {
  case "$1" in
    json|raw) printf 'json' ;;
    markdown) printf 'md' ;;
    *) printf 'txt' ;;
  esac
}

artifact_root() {
  local root="$1"
  printf '%s\n' "${KENT_MCP_ARTIFACT_ROOT:-$root/build/mcp-artifacts}"
}

append_call_log() {
  local root="$1"
  local server="$2"
  local tool="$3"
  local config="$4"
  local output_mode="$5"
  local raw_path="$6"
  local exit_code="$7"
  local error_code="$8"
  local safe_output_mode="${9:-raw}"
  local log_dir log_file

  log_dir="$(artifact_root "$root")/_mcp-log"
  log_file="$log_dir/mcporter-calls.jsonl"
  mkdir -p "$log_dir"

  python3 - "$log_file" "$server" "$tool" "$config" "$output_mode" \
    "$raw_path" "$exit_code" "$error_code" "$root" "$safe_output_mode" <<'PY'
import json
import sys
from datetime import datetime, timezone

(
    log_file,
    server,
    tool,
    config,
    output_mode,
    raw_path,
    exit_code,
    error_code,
    root,
    safe_output_mode,
) = sys.argv[1:]
record = {
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "workspace": root,
    "server": server,
    "tool": tool,
    "configPath": config,
    "outputMode": output_mode,
    "safeOutputMode": safe_output_mode,
    "rawOutputPath": raw_path or None,
    "exitCode": int(exit_code),
    "errorCode": error_code or None,
}

with open(log_file, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
}

mcp_memory_adapter() {
  printf '%s\n' "$HOME/.kent/bin/kent-project-memory"
}

mcp_validate_standalone_binding() {
  local binding="$1"
  [[ -n "$binding" ]] || return 0
  printf '%s' "$binding" | python3 -c '
import json
import re
import sys

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result

def reject_constant(_value):
    raise ValueError

try:
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError
    value = json.loads(raw.decode("utf-8", errors="strict"),
                       object_pairs_hook=unique_object,
                       parse_constant=reject_constant)
    if (not isinstance(value, dict)
            or set(value) != {"project_path", "project_uid", "task_key",
                              "task_uid", "task_binding_sha256"}
            or not isinstance(value["project_path"], str)
            or not value["project_path"].startswith("/")
            or not isinstance(value["project_uid"], str)
            or not value["project_uid"]
            or not isinstance(value["task_key"], str)
            or not value["task_key"]
            or not isinstance(value["task_uid"], str)
            or not value["task_uid"]
            or not isinstance(value["task_binding_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["task_binding_sha256"])
            or any("\x00" in item for item in value.values()
                   if isinstance(item, str))):
        raise ValueError
    print(json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False))
except (UnicodeError, ValueError, TypeError, json.JSONDecodeError,
        RecursionError):
    raise SystemExit(1)
'
}

mcp_make_permitted_metadata() {
  local operation="$1"
  local server="$2"
  local tool="$3"
  local config_sha256="$4"
  local output_mode="$5"
  local safe_mode="$6"
  local save_raw="$7"
  local value

  for value in "$server" "$tool" "$output_mode"; do
    if [[ -n "$value" && ! "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
      printf 'mcp_metadata_value_invalid: permitted metadata contains an unsupported value\n' >&2
      return 2
    fi
  done
  if [[ ! "$config_sha256" =~ ^[0-9a-f]{64}$ ||
    ( "$operation" != "call" && "$operation" != "list" ) ||
    ( "$safe_mode" != true && "$safe_mode" != false ) ||
    ( "$save_raw" != true && "$save_raw" != false ) ]]; then
    printf 'mcp_metadata_value_invalid: permitted metadata is malformed\n' >&2
    return 2
  fi

  jq -cnS \
    --arg operation "$operation" \
    --arg server "$server" \
    --arg tool "$tool" \
    --arg config_sha256 "$config_sha256" \
    --arg output_mode "$output_mode" \
    --argjson safe_mode "$safe_mode" \
    --argjson save_raw "$save_raw" \
    '{operation:$operation,server:$server,tool:$tool,
      config_sha256:$config_sha256,output_mode:$output_mode,
      safe_mode:$safe_mode,save_raw:$save_raw}'
}

mcp_config_sha256() {
  local config_display="$1"
  local config_path="$2"
  local content_sha=""

  if [[ -n "$config_path" && -f "$config_path" ]]; then
    content_sha="$(shasum -a 256 "$config_path" | awk '{print $1}')" || return
  fi
  mcp_sha256_fields "$config_display" "$config_path" "$content_sha"
}

mcp_memory_request_json() {
  local operation="$1"
  local workspace_path="$2"
  local binding_ref="$3"
  local standalone_binding="$4"
  shift 4

  {
    if [[ -n "${mcp_task_locator_json:-}" ]]; then
      set -- "$@" "task_locator" "json" "$mcp_task_locator_json"
    fi
    printf '%s\0' "$operation" "$workspace_path" "$binding_ref" \
      "$standalone_binding" "$@"
  } | python3 -c '
import json
import sys

parts = sys.stdin.buffer.read().split(b"\0")
if parts and parts[-1] == b"":
    parts.pop()
if len(parts) < 4 or (len(parts) - 4) % 3:
    raise SystemExit("invalid MCP request fields")
try:
    operation, workspace, binding_raw, standalone_raw = [
        value.decode("utf-8") for value in parts[:4]
    ]
    request = {
        "schema": "r2-mcp-request-v1",
        "operation": operation,
        "workspace_path": workspace,
    }
    if binding_raw:
        request["binding_ref"] = json.loads(binding_raw)
    if standalone_raw:
        request["standalone_binding"] = json.loads(standalone_raw)
    for offset in range(4, len(parts), 3):
        key, kind, raw = (value.decode("utf-8") for value in parts[offset:offset + 3])
        if kind == "json":
            value = json.loads(raw)
        elif kind == "int":
            value = int(raw)
        elif kind == "bool":
            if raw not in ("true", "false"):
                raise ValueError
            value = raw == "true"
        elif kind == "digest-or-null":
            if raw == "":
                value = None
            elif len(raw) == 64 and all(char in "0123456789abcdef" for char in raw):
                value = raw
            else:
                raise ValueError
        elif kind == "string":
            value = raw
        else:
            raise ValueError
        if key in request:
            raise ValueError
        request[key] = value
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    print(encoded)
except (UnicodeError, ValueError, TypeError, json.JSONDecodeError, RecursionError):
    raise SystemExit("invalid MCP request value")
'
}

mcp_memory_send_request() {
  local request="$1"
  local body_path="${2-}"
  local adapter response response_bytes status
  adapter="$(mcp_memory_adapter)"
  if [[ "$HOME" != /* || ! -x "$adapter" ]]; then
    printf 'mcp_project_memory_unavailable: %s is not executable\n' \
      "$adapter" >&2
    return 2
  fi

  if [[ -n "$body_path" ]]; then
    response="$(
      {
        printf '%s\n' "$request"
        cat "$body_path"
      } | "$adapter" mcp
    )" || {
      status=$?
      return "$status"
    }
  else
    response="$(printf '%s\n' "$request" | "$adapter" mcp)" || {
      status=$?
      return "$status"
    }
  fi

  response_bytes="$(printf '%s' "$response" | wc -c | tr -d '[:space:]')"
  if [[ ! "$response_bytes" =~ ^[0-9]+$ ]] ||
    ((response_bytes > 65536)); then
    printf 'mcp_project_memory_invalid_response: response exceeds limit\n' >&2
    return 2
  fi
  if ! jq -e '
    type == "object" and
    (keys == ["result","schema"]) and
    .schema == "r2-mcp-response-v1" and
    (.result | type == "object")
  ' <<<"$response" >/dev/null; then
    printf 'mcp_project_memory_invalid_response: unexpected response schema\n' >&2
    return 2
  fi
  printf '%s\n' "$response"
}

mcp_resolve_binding() {
  local workspace_path="$1"
  local standalone_binding="${2-}"
  local task_locator="${3-}"
  local request response mode binding_ref
  mcp_task_locator_json=""
  if [[ -n "$task_locator" ]]; then
    [[ -z "$standalone_binding" ]] || {
      printf 'mcp_task_locator_conflict: standalone binding is present\n' >&2
      return 2
    }
    mcp_task_locator_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$task_locator")" || return
  fi

  if [[ -n "$standalone_binding" ]]; then
    standalone_binding="$(
      mcp_validate_standalone_binding "$standalone_binding"
    )" || {
      printf 'mcp_standalone_binding_invalid: expected canonical task identity\n' >&2
      return 2
    }
  fi
  if [[ -n "$standalone_binding" && -n "${KENT_TASK_ID:-}" ]]; then
    printf 'mcp_standalone_binding_conflict: native Task context is present\n' >&2
    return 2
  fi

  mcp_workspace_path="$workspace_path"
  mcp_standalone_binding_json="$standalone_binding"
  request="$(
    mcp_memory_request_json "resolve-mcp" "$workspace_path" "" \
      "$standalone_binding"
  )" || return
  response="$(mcp_memory_send_request "$request")" || return
  mode="$(jq -er '.result.mode | select(. == "legacy" or . == "r2")' \
    <<<"$response")" || {
    printf 'mcp_project_memory_invalid_response: unknown task mode\n' >&2
    return 2
  }

  # shellcheck disable=SC2034
  mcp_mode="$mode"
  mcp_binding_ref_json=""
  mcp_binding_task_json_path=""
  if [[ "$mode" == "r2" ]]; then
    binding_ref="$(
      jq -ce '
        .result.binding_ref |
        select(type == "object" and keys == ["path","sha256"] and
          (.path | type == "string" and startswith("/") and
            endswith("/task.json")) and
          (.sha256 | type == "string" and test("^[0-9a-f]{64}$")))
      ' <<<"$response"
    )" || {
      printf 'mcp_project_memory_invalid_response: R2 binding missing\n' >&2
      return 2
    }
    mcp_binding_ref_json="$binding_ref"
    mcp_binding_task_json_path="$(jq -er '.path' <<<"$binding_ref")"
  fi
}

mcp_validate_invocation_id() {
  [[ "$1" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

mcp_sha256_fields() {
  {
    printf '%s\0' "$@"
  } | python3 -c '
import hashlib
import json
import sys

fields = sys.stdin.buffer.read().split(b"\0")
if fields and fields[-1] == b"":
    fields.pop()
try:
    values = [field.decode("utf-8") for field in fields]
    canonical = json.dumps(values, ensure_ascii=False, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
except (UnicodeError, ValueError):
    raise SystemExit("invalid MCP request fingerprint")
print(hashlib.sha256(canonical).hexdigest())
'
}

mcp_begin_invocation() {
  local invocation_id="$1"
  local request_sha256="$2"
  local metadata_json="$3"
  local request response state owner

  request="$(
    mcp_memory_request_json "begin-invocation" "$mcp_workspace_path" \
      "$mcp_binding_ref_json" "$mcp_standalone_binding_json" \
      invocation_id string "$invocation_id" \
      request_sha256 string "$request_sha256" \
      permitted_metadata json "$metadata_json"
  )" || return
  response="$(mcp_memory_send_request "$request")" || return
  state="$(jq -er '.result.state | select(. == "started" or . == "unknown" or . == "finished")' \
    <<<"$response")" || {
    printf 'mcp_project_memory_invalid_response: invocation state missing\n' >&2
    return 2
  }

  mcp_owner_token=""
  case "$state" in
    started)
      owner="$(
        jq -er '.result.owner_token | select(type == "string" and
          test("^[0-9a-f]{64}$"))' <<<"$response"
      )" || {
        printf 'mcp_project_memory_invalid_response: invocation owner missing\n' >&2
        return 2
      }
      mcp_owner_token="$owner"
      ;;
    finished)
      printf 'mcp_invocation_finished: invocation already has a terminal receipt; read-invocation %s\n' \
        "$invocation_id" >&2
      return 4
      ;;
    unknown)
      printf 'mcp_invocation_unknown: started receipt has no terminal result; read-invocation %s; do not replay\n' \
        "$invocation_id" >&2
      return 5
      ;;
  esac
}

mcp_put_artifact() {
  local invocation_id="$1"
  local kind="$2"
  local body_path="$3"
  local byte_size sha256 request response

  byte_size="$(wc -c <"$body_path" | tr -d '[:space:]')"
  sha256="$(shasum -a 256 "$body_path" | awk '{print $1}')"
  if [[ ! "$byte_size" =~ ^[0-9]+$ ||
    ! "$sha256" =~ ^[0-9a-f]{64}$ ]] ||
    ((byte_size > 8388608)); then
    printf 'mcp_artifact_invalid: raw MCP output exceeds the bounded object size\n' >&2
    return 2
  fi
  request="$(
    mcp_memory_request_json "put-artifact" "$mcp_workspace_path" \
      "$mcp_binding_ref_json" "$mcp_standalone_binding_json" \
      invocation_id string "$invocation_id" \
      owner_token string "$mcp_owner_token" \
      kind string "$kind" \
      sha256 string "$sha256" \
      byte_size int "$byte_size"
  )" || return
  response="$(mcp_memory_send_request "$request" "$body_path")" || return
  mcp_object_sha256="$(
    jq -er '.result.object_sha256 | select(type == "string" and
      test("^[0-9a-f]{64}$"))' <<<"$response"
  )" || {
    printf 'mcp_project_memory_invalid_response: object digest missing\n' >&2
    return 2
  }
  mcp_object_path="$(
    jq -er '.result.object_path | select(type == "string" and startswith("/"))' \
      <<<"$response"
  )" || {
    printf 'mcp_project_memory_invalid_response: object locator missing\n' >&2
    return 2
  }
  [[ "$mcp_object_sha256" == "$sha256" ]] || {
    printf 'mcp_project_memory_invalid_response: object digest differs\n' >&2
    return 2
  }
  mcp_validate_bound_object "$mcp_object_sha256" "$mcp_object_path" || return
}

mcp_validate_bound_object() {
  local digest="$1"
  local object_path="$2"
  local task_dir object_dir actual_digest

  [[ "$digest" =~ ^[0-9a-f]{64}$ &&
    -n "$mcp_binding_task_json_path" &&
    "$mcp_binding_task_json_path" == */tasks/*/task.json ]] || {
    printf 'mcp_project_memory_invalid_response: object binding is malformed\n' >&2
    return 2
  }
  task_dir="${mcp_binding_task_json_path%/task.json}"
  object_dir="$task_dir/artifacts/mcp/objects"
  if [[ "$object_path" != "$object_dir/$digest" ||
    ! -f "$object_path" || -L "$object_path" ]]; then
    printf 'mcp_project_memory_invalid_response: object is outside the bound task\n' >&2
    return 2
  fi
  actual_digest="$(shasum -a 256 "$object_path" | awk '{print $1}')" || return
  [[ "$actual_digest" == "$digest" ]] || {
    printf 'mcp_project_memory_invalid_response: object content digest differs\n' >&2
    return 2
  }
}

mcp_finish_invocation() {
  local invocation_id="$1"
  local exit_code="$2"
  local error_code="$3"
  local object_sha256="${4-}"
  local request response state

  request="$(
    mcp_memory_request_json "finish-invocation" "$mcp_workspace_path" \
      "$mcp_binding_ref_json" "$mcp_standalone_binding_json" \
      invocation_id string "$invocation_id" \
      owner_token string "$mcp_owner_token" \
      exit_code int "$exit_code" \
      error_code string "$error_code" \
      object_sha256 digest-or-null "$object_sha256"
  )" || return
  response="$(mcp_memory_send_request "$request")" || return
  state="$(jq -er '.result.state | select(. == "finished")' \
    <<<"$response")" || {
    printf 'mcp_project_memory_invalid_response: terminal receipt missing\n' >&2
    return 2
  }
  [[ "$state" == "finished" ]]
}

mcp_read_cache() {
  local cache_key="$1"
  local request response state
  request="$(
    mcp_memory_request_json "read-cache" "$mcp_workspace_path" \
      "$mcp_binding_ref_json" "$mcp_standalone_binding_json" \
      cache_key string "$cache_key"
  )" || return
  response="$(mcp_memory_send_request "$request")" || return
  state="$(jq -er '.result.state | select(. == "hit" or . == "miss")' \
    <<<"$response")" || {
    printf 'mcp_project_memory_invalid_response: cache state missing\n' >&2
    return 2
  }
  # shellcheck disable=SC2034
  mcp_cache_state="$state"
  mcp_cache_object_sha256=""
  mcp_cache_object_path=""
  if [[ "$state" == "hit" ]]; then
    mcp_cache_object_sha256="$(
      jq -er '.result.object_sha256 | select(type == "string" and
        test("^[0-9a-f]{64}$"))' <<<"$response"
    )" || {
      printf 'mcp_project_memory_invalid_response: cache object digest missing\n' >&2
      return 2
    }
    mcp_cache_object_path="$(
      jq -er '.result.object_path | select(type == "string" and startswith("/"))' \
        <<<"$response"
    )" || {
      printf 'mcp_project_memory_invalid_response: cache object locator missing\n' >&2
      return 2
    }
    mcp_validate_bound_object "$mcp_cache_object_sha256" \
      "$mcp_cache_object_path" || return
  fi
}

mcp_validate_schema_output() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import math
import sys

MAX_BYTES = 8 * 1024 * 1024


def reject_constant(_value):
    raise ValueError


def parse_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


try:
    with open(sys.argv[1], "rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError
    document = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=parse_float,
    )
    if not isinstance(document, dict):
        raise ValueError
except (OSError, UnicodeError, ValueError, json.JSONDecodeError,
        RecursionError):
    raise SystemExit(1)
PY
}

mcp_publish_cache() {
  local invocation_id="$1"
  local cache_key="$2"
  local object_sha256="$3"
  local request response state
  request="$(
    mcp_memory_request_json "publish-cache" "$mcp_workspace_path" \
      "$mcp_binding_ref_json" "$mcp_standalone_binding_json" \
      invocation_id string "$invocation_id" \
      owner_token string "$mcp_owner_token" \
      cache_key string "$cache_key" \
      object_sha256 string "$object_sha256"
  )" || return
  response="$(mcp_memory_send_request "$request")" || return
  state="$(jq -er '.result.state | select(. == "published")' \
    <<<"$response")" || {
    printf 'mcp_project_memory_invalid_response: cache publication missing\n' >&2
    return 2
  }
  [[ "$state" == "published" ]]
}

with_server_lock_if_needed() {
  local server="$1"
  local root="$2"
  shift 2

  if [[ "$server" != "serena" ]]; then
    "$@"
    return
  fi

  local lock_dir lock_file
  lock_dir="$root/.serena"
  lock_file="$lock_dir/mcp-serena.lock"
  mkdir -p "$lock_dir"

  if command -v flock >/dev/null 2>&1; then
    (
      flock 9
      "$@"
    ) 9>"$lock_file"
    return
  fi

  if command -v lockf >/dev/null 2>&1; then
    lockf "$lock_file" "$@"
    return
  fi

  printf 'serena_lock_unavailable: flock/lockf not found; running without serialization\n' >&2
  "$@"
}

resolve_serena_command() {
  local config="$1"
  local configured

  if [[ -n "${SERENA_COMMAND:-}" ]]; then
    printf '%s\n' "$SERENA_COMMAND"
    return
  fi
  if [[ -n "$config" && -f "$config" ]]; then
    configured="$(
      python3 - "$config" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
    print(data.get("mcpServers", {}).get("serena", {}).get("command", ""))
except Exception:
    print("")
PY
    )"
    if [[ -n "$configured" ]]; then
      printf '%s\n' "$configured"
      return
    fi
  fi
  if command -v serena >/dev/null 2>&1; then
    command -v serena
  else
    printf '%s\n' "$HOME/.local/bin/serena"
  fi
}

serena_stdio_args() {
  local root="$1"
  printf '%s\0' \
    --stdio-arg start-mcp-server \
    --stdio-arg --context=ide \
    --stdio-arg --project \
    --stdio-arg "$root" \
    --stdio-arg --enable-web-dashboard=false \
    --stdio-arg --open-web-dashboard=false \
    --cwd "$root" \
    --name serena
}

resolve_project_server_command() {
  local root="$1"
  local server="$2"
  local primary candidate

  primary="$(primary_workspace_root "$root")"
  for candidate in \
    "$root/.kent/adapters/mcp/servers/$server" \
    "$root/.kent/adapters/mcp/${server}-server.sh" \
    "$primary/.kent/adapters/mcp/servers/$server" \
    "$primary/.kent/adapters/mcp/${server}-server.sh"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

resolve_project_policy_hook() {
  local root="$1"
  local primary candidate

  if [[ -n "${KENT_MCP_POLICY_HOOK:-}" ]]; then
    [[ -x "$KENT_MCP_POLICY_HOOK" ]] || {
      printf 'mcp_policy_hook_invalid: not executable: %s\n' \
        "$KENT_MCP_POLICY_HOOK" >&2
      return 2
    }
    printf '%s\n' "$KENT_MCP_POLICY_HOOK"
    return
  fi

  primary="$(primary_workspace_root "$root")"
  for candidate in \
    "$root/.kent/adapters/mcp/policy" \
    "$primary/.kent/adapters/mcp/policy"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  return 1
}

project_server_stdio_args() {
  local root="$1"
  local server="$2"
  printf '%s\0' --cwd "$root" --name "$server"
}
