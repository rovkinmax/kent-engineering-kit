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
  printf '%s\n' "${KENT_MCP_ARTIFACT_ROOT:-$root/.todo}"
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
      run_serena_command_with_cleanup "$root" "$@"
    ) 9>"$lock_file"
    return
  fi

  if command -v lockf >/dev/null 2>&1; then
    printf 'serena_lock_cleanup_limited: flock not found; lockf fallback cannot cleanup Kotlin LSP orphans\n' >&2
    lockf "$lock_file" "$@"
    return
  fi

  printf 'serena_lock_unavailable: flock/lockf not found; running without serialization\n' >&2
  "$@"
}

run_serena_command_with_cleanup() {
  local root="$1"
  shift

  cleanup_orphan_kotlin_lsp_processes_for_root "$root"
  set +e
  "$@"
  local exit_code=$?
  set -e
  cleanup_orphan_kotlin_lsp_processes_for_root "$root"
  return "$exit_code"
}

cleanup_orphan_kotlin_lsp_processes_for_root() {
  local root="$1"
  local physical_root pid ppid
  local killed_pids=()

  [[ "${SERENA_SKIP_LSP_CLEANUP:-}" != "1" ]] || return 0
  command -v lsof >/dev/null 2>&1 || return 0

  physical_root="$(cd "$root" && pwd -P)"
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    [[ "$(kotlin_lsp_cwd "$pid")" == "$physical_root" ]] || continue
    ppid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$ppid" == "1" ]] || continue
    if kill "$pid" 2>/dev/null; then
      killed_pids+=("$pid")
    fi
  done < <(pgrep -f 'com.jetbrains.ls.kotlinLsp.KotlinLspServerKt --stdio' 2>/dev/null || true)

  wait_for_process_shutdown "${killed_pids[@]}"
}

kotlin_lsp_cwd() {
  local pid="$1"
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null |
    sed -n 's/^n//p' |
    tail -n 1
}

wait_for_process_shutdown() {
  local pids=("$@")
  local pid
  local alive=()

  [[ "${#pids[@]}" -gt 0 ]] || return 0
  for _ in {1..10}; do
    alive=()
    for pid in "${pids[@]}"; do
      kill -0 "$pid" 2>/dev/null && alive+=("$pid")
    done
    [[ "${#alive[@]}" -eq 0 ]] && return 0
    sleep 0.2
  done

  for pid in "${alive[@]}"; do
    kill -9 "$pid" 2>/dev/null || true
  done
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
