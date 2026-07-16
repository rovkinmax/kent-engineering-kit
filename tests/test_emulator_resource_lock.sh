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
cat <<'DEVICES'
List of devices attached
emulator-5554	device product:sdk model:sdk transport_id:1
physical-123	device product:phone model:phone transport_id:2
emulator-5556	offline product:sdk model:sdk transport_id:3

DEVICES
ADB
chmod +x "$temporary/bin/adb"
export PATH="$temporary/bin:$PATH"

token="$("$adapter" acquire emulator-5554 0 7200)"
[[ -n "$token" ]]
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

[[ "$("$adapter" adb-emulators)" == "emulator-5554" ]]
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
