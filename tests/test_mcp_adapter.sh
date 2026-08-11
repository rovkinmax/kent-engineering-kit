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
mkdir -p "$home/.kent" "$home/.mcporter" "$fake_bin" "$main"

git -C "$main" init -q
git -C "$main" config user.name "Kent Test"
git -C "$main" config user.email "kent@example.invalid"
echo tracked >"$main/tracked"
git -C "$main" add tracked
git -C "$main" commit -qm "Initial"
git -C "$main" worktree add -qb task "$worktree"

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
printf '%s\n' '{"ok":true}'
SH
chmod +x "$fake_bin/mcporter"

run_adapter() {
  local log="$1"
  shift
  (
    cd "$worktree"
    HOME="$home" \
      PATH="$fake_bin:$PATH" \
      MCPORTER_ARGS_LOG="$log" \
      "$@"
  )
}

"$call_adapter" --self-test

args_log="$tmp/call-args"
run_adapter "$args_log" "$call_adapter" example.inspect --allow-mutate >/dev/null
grep -Fx -- "--config" "$args_log" >/dev/null
grep -Fx -- "$main/.mcp.json" "$args_log" >/dev/null
grep -Fx -- "--root" "$args_log" >/dev/null
grep -Fx -- "$worktree" "$args_log" >/dev/null
grep -Fx -- "example.inspect" "$args_log" >/dev/null
if find "$worktree/.todo/_mcp-raw" -type f -print -quit 2>/dev/null |
  grep -q .; then
  echo "MCP call persisted raw output without explicit opt-in" >&2
  exit 1
fi
jq -e '.rawOutputPath == null' \
  "$worktree/.todo/_mcp-log/mcporter-calls.jsonl" >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --save-raw >/dev/null
raw_file="$(
  find "$worktree/.todo/_mcp-raw/example" -type f -print -quit
)"
test -s "$raw_file"
tail -1 "$worktree/.todo/_mcp-log/mcporter-calls.jsonl" |
  jq -e --arg path "$raw_file" '.rawOutputPath == $path' >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --quiet >"$tmp/quiet.out"
test ! -s "$tmp/quiet.out"
tail -1 "$worktree/.todo/_mcp-log/mcporter-calls.jsonl" |
  jq -e '.safeOutputMode == "quiet" and .rawOutputPath == null' >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --digest-output >"$tmp/digest.out"
jq -e '.status == "passed" and (.sha256 | length) == 64' \
  "$tmp/digest.out" >/dev/null
if grep -Fq '"ok":true' "$tmp/digest.out"; then
  echo "digest output leaked raw MCP content" >&2
  exit 1
fi

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --assert-contains '"ok":true' \
  --assert-not-contains '"secret"' >"$tmp/assert.out"
jq -e '.status == "passed" and .contains == 1 and .notContains == 1' \
  "$tmp/assert.out" >/dev/null
if run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --assert-contains '"missing"' >"$tmp/assert-fail.out"; then
  echo "failed MCP output assertion unexpectedly succeeded" >&2
  exit 1
fi
jq -e '.status == "failed" and .failedContains == 1' \
  "$tmp/assert-fail.out" >/dev/null

run_adapter "$args_log" "$call_adapter" example.inspect \
  --allow-mutate --hash-matches 'ok|missing' \
  --marker-present '"ok":true' \
  --marker-present '"secret"' >"$tmp/extract.out"
jq -e '
  .status == "passed" and
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
jq -e '.status == "passed" and (.sha256 | length) == 64' \
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
  .status == "passed" and
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

echo "MCP adapter tests passed"
