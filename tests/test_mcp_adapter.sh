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

safe_mobile_result_complete() {
  [[ -s "$1" ]] || return 1
  jq -e '
    type == "object" and
    .schema == "kent-mcp-result-v1" and
    (.transport == "succeeded" or .transport == "failed") and
    (.processing == "succeeded" or .processing == "failed" or
      .processing == "not_attempted") and
    .action == "unknown" and
    (.assertion == "passed" or .assertion == "failed" or
      .assertion == "unknown" or .assertion == "not_requested") and
    (.assertionKind == "literal" or .assertionKind == "json_boolean" or
      .assertionKind == "none")
  ' "$1" >/dev/null
}

mobile_checkpoint_gate() {
  local result="$1"
  local interaction_evidence="$2"
  local schema_known="$3"
  local error_conditions_covered="$4"

  [[ "$interaction_evidence" == true ]] || return 1
  [[ "$schema_known" == true ]] || return 1
  [[ "$error_conditions_covered" == true ]] || return 1
  safe_mobile_result_complete "$result" || return 1
  jq -e '
    .transport == "succeeded" and
    .processing == "succeeded" and
    .action == "unknown" and
    .assertion == "passed" and
    .assertionKind == "json_boolean" and
    .assertionCount > 0 and
    (.assertionResults | length) == .assertionCount and
    all(.assertionResults[]; . == "passed")
  ' "$result" >/dev/null
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
tail -1 "$worktree/.todo/_mcp-log/mcporter-calls.jsonl" |
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
  "$worktree/.todo/_mcp-log/mcporter-calls.jsonl" \
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
if find "$worktree/.todo/_mcp-raw/mobile" -type f -print -quit \
  2>/dev/null | grep -q .; then
  echo "safe mobile output persisted a raw artifact" >&2
  exit 1
fi
tail -1 "$worktree/.todo/_mcp-log/mcporter-calls.jsonl" |
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

# This synthetic response reproduces the delivered false-marker defect:
# transport/output processing succeeds although the expected destination is
# absent. The fixture identity is its fixed, non-secret JSON value.
cat >"$tmp/mobile-negative.json" <<'JSON'
{"destination":"phone_login","error":false}
JSON
fixture_sha256="$(shasum -a 256 "$tmp/mobile-negative.json" | awk '{print $1}')"
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

complete_gate_status=0
safe_mobile_result_complete "$tmp/mobile-negative-result.json" ||
  complete_gate_status=$?
checkpoint_gate_status=0
mobile_checkpoint_gate "$tmp/mobile-negative-result.json" true true true ||
  checkpoint_gate_status=$?
no_interaction_gate_status=0
mobile_checkpoint_gate "$tmp/mobile-negative-result.json" false true true ||
  no_interaction_gate_status=$?
if ((checkpoint_gate_status == 0 || no_interaction_gate_status == 0)); then
  echo "RED regression: processing-only mobile result promoted a checkpoint" >&2
  exit 1
fi
if ((complete_gate_status != 0)); then
  echo "RED fixture=mobile-negative-v1 input_sha256=$fixture_sha256 adapter_exit=0 safe_mode=extract marker=false result_schema=missing checkpoint_gate=reject interaction_gate=reject" >&2
  exit 1
fi

if ! mobile_checkpoint_gate "$tmp/json-true.out" true true true; then
  echo "positive known-schema result with interaction evidence was rejected" >&2
  exit 1
fi
if mobile_checkpoint_gate "$tmp/json-true.out" false true true; then
  echo "checkpoint gate accepted missing interaction evidence" >&2
  exit 1
fi

for non_promoting_result in \
  "$tmp/json-false.out" \
  "$tmp/json-missing.out" \
  "$tmp/json-true-and-missing.out" \
  "$tmp/json-false-and-missing.out" \
  "$tmp/quiet.out" \
  "$tmp/mobile-digest.out" \
  "$tmp/mobile-extract.out" \
  "$tmp/assert.out" \
  "$tmp/mobile-transport-failure.out" \
  "$tmp/json-duplicate-keys.out" \
  "$tmp/mobile-negative-result.json"; do
  if mobile_checkpoint_gate "$non_promoting_result" true true true; then
    echo "checkpoint gate promoted a negative, unknown, or non-semantic result" >&2
    exit 1
  fi
done

# The consumer gate receives only adapter output and declared contract
# capabilities, never the fixture response body.
cat >"$tmp/request-echo-in-error.json" <<'JSON'
{"error":"failed","request":{"authenticated":true}}
JSON
MCPORTER_FIXTURE_FILE="$tmp/request-echo-in-error.json" \
  run_mobile_tree --assert-json-true /request/authenticated \
    >"$tmp/request-echo-in-error.out"
jq -e '.assertion == "passed" and .assertionResults == ["passed"]' \
  "$tmp/request-echo-in-error.out" >/dev/null
if mobile_checkpoint_gate "$tmp/request-echo-in-error.out" true false false; then
  echo "checkpoint gate promoted request echo from an error result" >&2
  exit 1
fi

cat >"$tmp/positive-with-contradictory-error.json" <<'JSON'
{"authenticated":true,"error":true}
JSON
MCPORTER_FIXTURE_FILE="$tmp/positive-with-contradictory-error.json" \
  run_mobile_tree --assert-json-true /authenticated \
    >"$tmp/positive-with-contradictory-error.out"
jq -e '.assertion == "passed" and .assertionResults == ["passed"]' \
  "$tmp/positive-with-contradictory-error.out" >/dev/null
if mobile_checkpoint_gate \
  "$tmp/positive-with-contradictory-error.out" true true false; then
  echo "checkpoint gate promoted a positive assertion with uncovered error condition" >&2
  exit 1
fi

echo "MCP adapter tests passed"
