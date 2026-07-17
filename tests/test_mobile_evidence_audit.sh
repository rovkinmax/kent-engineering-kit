#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter="$repo_root/templates/project/mobile-evidence-audit.sh"
temporary="$(mktemp -d)"
temporary="$(cd "$temporary" && pwd -P)"
trap 'find "$temporary" -depth -delete' EXIT
package_name="com.example.app"

expect_unsafe() {
  local directory="$1"
  local reason="$2"
  local output audit_status

  set +e
  output="$("$adapter" "$directory" "$package_name" 2>&1)"
  audit_status=$?
  set -e
  [[ "$audit_status" -eq 65 ]]
  [[ "$output" == *"$reason"* ]]
  printf '%s\n' "$output"
}

safe_dir="$temporary/safe"
mkdir -p "$safe_dir"
printf 'No package-specific fatal or ANR signal was found.\n' \
  >"$safe_dir/smoke-report.md"
printf '%s\n' \
  '07-16 12:00:00.000 100 200 E ActivityManager: ANR in com.example.app' \
  >"$safe_dir/fatal-anr-summary.txt"
safe_output="$("$adapter" "$safe_dir" "$package_name")"
[[ "$safe_output" == *"evidence_audit_status=passed"* ]]
[[ "$safe_output" == *"files_scanned=2"* ]]

logcat_dir="$temporary/logcat"
mkdir -p "$logcat_dir"
printf 'Harmless content still represents a forbidden broad-log artifact.\n' \
  >"$logcat_dir/full-device-logcat.txt"
logcat_output="$(expect_unsafe "$logcat_dir" "broad_log_filename")"
[[ "$logcat_output" == *"full-device-logcat.txt"* ]]

neutral_log_dir="$temporary/neutral-log"
mkdir -p "$neutral_log_dir"
printf '%s\n' \
  '07-16 12:00:00.000 100 200 I OtherTag: unrelated device activity' \
  >"$neutral_log_dir/runtime-output.txt"
expect_unsafe "$neutral_log_dir" "unscoped_logcat_content" >/dev/null

non_signal_dir="$temporary/non-signal"
mkdir -p "$non_signal_dir"
printf '%s\n' \
  '07-16 12:00:00.000 100 200 I App: com.example.app regular output' \
  >"$non_signal_dir/runtime-output.txt"
expect_unsafe "$non_signal_dir" "non_signal_logcat_content" >/dev/null

auth_dir="$temporary/auth"
mkdir -p "$auth_dir"
auth_secret='do-not-print-basic-secret'
printf '{"authorization":"Basic %s"}\n' "$auth_secret" \
  >"$auth_dir/runtime.json"
auth_output="$(expect_unsafe "$auth_dir" "sensitive_content_marker")"
[[ "$auth_output" != *"$auth_secret"* ]]

token_dir="$temporary/token"
mkdir -p "$token_dir"
token_secret='do-not-print-token-secret'
printf '{"accessToken":"%s"}\n' "$token_secret" \
  >"$token_dir/runtime.json"
token_output="$(expect_unsafe "$token_dir" "sensitive_content_marker")"
[[ "$token_output" != *"$token_secret"* ]]

binary_dir="$temporary/binary"
mkdir -p "$binary_dir"
binary_secret='do-not-print-binary-secret'
printf '\000authorization: Bearer %s\000' "$binary_secret" \
  >"$binary_dir/runtime.bin"
binary_output="$(expect_unsafe "$binary_dir" "sensitive_content_marker")"
[[ "$binary_output" != *"$binary_secret"* ]]

symlink_dir="$temporary/symlink"
mkdir -p "$symlink_dir"
printf 'outside\n' >"$temporary/outside.txt"
ln -s "$temporary/outside.txt" "$symlink_dir/external.txt"
expect_unsafe "$symlink_dir" "symlink_not_allowed" >/dev/null

ln -s "$safe_dir" "$temporary/final-link"
set +e
final_link_output="$(
  "$adapter" "$temporary/final-link/" "$package_name" 2>&1
)"
final_link_status=$?
set -e
[[ "$final_link_status" -eq 64 ]]
[[ "$final_link_output" == *"must not contain symlinks"* ]]

mkdir -p "$temporary/parent-target/evidence"
printf 'safe\n' >"$temporary/parent-target/evidence/report.txt"
ln -s "$temporary/parent-target" "$temporary/parent-link"
set +e
parent_link_output="$(
  "$adapter" "$temporary/parent-link/evidence" "$package_name" 2>&1
)"
parent_link_status=$?
set -e
[[ "$parent_link_status" -eq 64 ]]
[[ "$parent_link_output" == *"must not contain symlinks"* ]]

special_dir="$temporary/special"
mkdir -p "$special_dir"
mkfifo "$special_dir/runtime.pipe"
expect_unsafe "$special_dir" "unsupported_file_type" >/dev/null

echo "mobile evidence audit tests passed"
