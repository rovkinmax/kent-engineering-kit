#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter="$repo_root/templates/project/emulator-resource-lock.sh"
temporary="$(mktemp -d)"
trap 'find "$temporary" -depth -delete' EXIT

export KENT_RESOURCE_LOCK_DIR="$temporary/locks"
mkdir -p "$temporary/bin"

cat >"$temporary/bin/adb" <<'ADB'
#!/usr/bin/env bash
if [[ "${1:-}" == "-s" ]]; then
  serial="$2"
  shift 2
  if [[ "${1:-}" == "shell" && "${2:-}" == "getprop" ]]; then
    property="${3:-}"
    cat >/dev/null
    case "$serial:$property" in
      emulator-5554:ro.build.characteristics) printf 'emulator\n' ;;
      emulator-5554:ro.product.model) printf 'sdk_google_atv64_amati_arm64\n' ;;
      emulator-5554:ro.product.name) printf 'sdk_google_atv64_amati_arm64\n' ;;
      emulator-5556:ro.build.characteristics) printf 'emulator\n' ;;
      emulator-5556:ro.product.model) printf 'sdk_gphone64_arm64\n' ;;
      emulator-5556:ro.product.name) printf 'sdk_gphone64_arm64\n' ;;
      *) exit 1 ;;
    esac
    exit 0
  fi
fi
cat <<'DEVICES'
List of devices attached
emulator-5554	device product:sdk model:sdk transport_id:1
physical-123	device product:phone model:phone transport_id:2
emulator-5556	device product:sdk model:sdk transport_id:3
emulator-5558	offline product:sdk model:sdk transport_id:4

DEVICES
ADB
chmod +x "$temporary/bin/adb"
export PATH="$temporary/bin:$PATH"

token="$(
  KENT_RESOURCE_LOCK_OWNER_PID=424242 \
    "$adapter" acquire emulator-5554 0 7200
)"
[[ -n "$token" ]]
owner_file="$KENT_RESOURCE_LOCK_DIR/mobile-emulator-5554.lock/owner"
[[ "$(sed -n 's/^pid=//p' "$owner_file")" == "424242" ]]
status_output="$("$adapter" status emulator-5554)"
[[ "$(printf '%s\n' "$status_output" | head -1)" == "locked" ]]
[[ "$status_output" == *"token=<redacted>"* ]]
[[ "$status_output" != *"$token"* ]]

set +e
"$adapter" acquire emulator-5554 0 7200 >/dev/null 2>&1
busy_status=$?
"$adapter" release emulator-5554 wrong-token >/dev/null 2>&1
wrong_token_status=$?
set -e
[[ "$busy_status" -eq 75 ]]
[[ "$wrong_token_status" -eq 64 ]]
[[ "$("$adapter" status emulator-5554 | head -1)" == "locked" ]]

"$adapter" release emulator-5554 "$token"
[[ "$("$adapter" status emulator-5554)" == "unlocked" ]]

first_token="$("$adapter" acquire emulator-5554 0 7200)"
selection="$("$adapter" acquire-any emulator-5554 emulator-5556 -- 0 7200)"
[[ "$(printf '%s\n' "$selection" | sed -n 's/^resource=//p')" == "emulator-5556" ]]
second_token="$(printf '%s\n' "$selection" | sed -n 's/^token=//p')"
"$adapter" release emulator-5554 "$first_token"
"$adapter" release emulator-5556 "$second_token"

resume_token="$("$adapter" acquire emulator-5559 0 7200)"
[[ "$("$adapter" resume emulator-5559 "$resume_token")" == "$resume_token" ]]
[[ "$("$adapter" status emulator-5559 | head -1)" == "locked" ]]
set +e
"$adapter" resume emulator-5559 wrong-token >/dev/null 2>&1
resume_mismatch_status=$?
set -e
[[ "$resume_mismatch_status" -eq 75 ]]
"$adapter" release emulator-5559 "$resume_token"

owned_token="$(
  KENT_TASK_ID=task-1 \
    KENT_SESSION_ID=session-1 \
    "$adapter" acquire emulator-5562 0 7200
)"
resumed_owned_token="$(
  KENT_TASK_ID=task-1 \
    KENT_SESSION_ID=session-2 \
    KENT_RESOURCE_LOCK_OWNER_PID=515151 \
    "$adapter" resume-owned emulator-5562
)"
[[ "$resumed_owned_token" == "$owned_token" ]]
owned_owner_file="$KENT_RESOURCE_LOCK_DIR/mobile-emulator-5562.lock/owner"
[[ "$(sed -n 's/^pid=//p' "$owned_owner_file")" == "515151" ]]
[[ "$(sed -n 's/^task_id=//p' "$owned_owner_file")" == "task-1" ]]
[[ "$(sed -n 's/^session_id=//p' "$owned_owner_file")" == "session-2" ]]
set +e
KENT_TASK_ID=task-2 \
  "$adapter" resume-owned emulator-5562 >/dev/null 2>&1
other_task_resume_status=$?
KENT_TASK_ID=unknown \
  "$adapter" resume-owned emulator-5562 >/dev/null 2>&1
unknown_task_resume_status=$?
KENT_TASK_ID=task-1 \
  "$adapter" resume-owned emulator-5563 >/dev/null 2>&1
absent_lock_resume_status=$?
set -e
[[ "$other_task_resume_status" -eq 75 ]]
[[ "$unknown_task_resume_status" -eq 64 ]]
[[ "$absent_lock_resume_status" -eq 75 ]]
[[ "$("$adapter" status emulator-5562 | head -1)" == "locked" ]]
"$adapter" release emulator-5562 "$owned_token"

released_resume_token="resume-after-release-token"
[[ "$("$adapter" resume emulator-5561 "$released_resume_token")" == "$released_resume_token" ]]
[[ "$("$adapter" status emulator-5561 | head -1)" == "locked" ]]
"$adapter" release emulator-5561 "$released_resume_token"

contention_dir="$temporary/contention"
mkdir -p "$contention_dir"
for contender in {1..8}; do
  (
    set +e
    "$adapter" acquire emulator-5560 0 7200 \
      >"$contention_dir/token-$contender" \
      2>"$contention_dir/error-$contender"
    printf '%s\n' "$?" >"$contention_dir/status-$contender"
    exit 0
  ) &
done
wait

success_count="$(grep -l '^0$' "$contention_dir"/status-* | wc -l | tr -d ' ')"
busy_count="$(grep -l '^75$' "$contention_dir"/status-* | wc -l | tr -d ' ')"
[[ "$success_count" -eq 1 ]]
[[ "$busy_count" -eq 7 ]]
winner_status="$(grep -l '^0$' "$contention_dir"/status-*)"
winner="${winner_status##*-}"
winner_token="$(cat "$contention_dir/token-$winner")"
"$adapter" release emulator-5560 "$winner_token"

[[ "$("$adapter" adb-emulators)" == $'emulator-5554\nemulator-5556' ]]
[[ "$("$adapter" adb-emulators tv)" == "emulator-5554" ]]
[[ "$("$adapter" adb-emulators phone)" == "emulator-5556" ]]
set +e
"$adapter" adb-emulators toaster >/dev/null 2>&1
invalid_form_factor_status=$?
set -e
[[ "$invalid_form_factor_status" -eq 64 ]]
[[ "$("$adapter" adb-physical-devices)" == "physical-123" ]]

stale_token="$("$adapter" acquire emulator-5554 0 7200)"
stale_dir="$KENT_RESOURCE_LOCK_DIR/mobile-emulator-5554.lock"
printf '1\n' >"$stale_dir/created_at"
replacement_file="$temporary/replacement-token"
"$adapter" acquire emulator-5554 5 1 \
  >"$replacement_file" \
  2>"$temporary/stale-reclaim.log" &
replacement_pid=$!
sleep 0.1
set +e
"$adapter" release emulator-5554 "$stale_token" >/dev/null 2>&1
stale_release_status=$?
set -e
wait "$replacement_pid"
replacement_token="$(cat "$replacement_file")"
[[ "$replacement_token" != "$stale_token" ]]
[[ "$stale_release_status" -eq 0 || "$stale_release_status" -eq 64 ]]
[[ "$("$adapter" status emulator-5554 | head -1)" == "locked" ]]
"$adapter" release emulator-5554 "$replacement_token"

if command -v lockf >/dev/null 2>&1; then
  lockf_token="$(
    KENT_RESOURCE_LOCK_BACKEND=lockf \
      "$adapter" acquire emulator-5558 0 7200
  )"
  [[ -n "$lockf_token" ]]
  [[ "$(
    KENT_RESOURCE_LOCK_BACKEND=lockf \
      "$adapter" status emulator-5558 | head -1
  )" == "locked" ]]
  KENT_RESOURCE_LOCK_BACKEND=lockf \
    "$adapter" release emulator-5558 "$lockf_token"
fi

echo "emulator resource lock tests passed"
