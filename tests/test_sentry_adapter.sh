#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter="$repo_root/templates/project/sentry-issues.sh"
tmp="$(mktemp -d)"
trap 'rm -r "$tmp"' EXIT

project="$tmp/project"
home="$tmp/home"
fake_bin="$tmp/bin"
mkdir -p "$project/.kent" "$home" "$fake_bin"
git -C "$project" init -q

cat >"$project/.kent/workflow-profile.toml" <<'EOF_PROFILE'
[integrations.sentry]
base_url = "https://sentry.example.invalid/"
organization = "acme"
project = "mobile"
credential_namespace = "ACME"
EOF_PROFILE

cat >"$fake_bin/curl" <<'EOF_CURL'
#!/usr/bin/env bash
set -euo pipefail
: "${FAKE_CURL_LOG:?}"
: "${FAKE_CURL_STATE:?}"
config="$(cat)"
printf 'config=%s\n' "$config" >>"$FAKE_CURL_LOG"
printf '%s\n' "$@" >>"$FAKE_CURL_LOG"
method=GET
url=""
body=""
previous=""
for argument in "$@"; do
  if [[ "$previous" == "--request" ]]; then
    method="$argument"
  elif [[ "$previous" == "--data" ]]; then
    body="$argument"
  fi
  url="$argument"
  previous="$argument"
done
printf 'CALL %s %s %s\n' "$method" "$url" "$body" >>"$FAKE_CURL_STATE"

if [[ "$method" == PUT ]]; then
  if [[ "$body" == *'"hasSeen":true'* ]]; then
    printf 'true\n' >"$FAKE_CURL_SEEN"
  else
    printf 'false\n' >"$FAKE_CURL_SEEN"
  fi
  printf '{}\n'
  exit 0
fi

issue_json() {
  local status="${FAKE_SENTRY_STATUS:-unresolved}"
  local seen=false
  [[ -f "${FAKE_CURL_SEEN:-}" ]] && seen="$(cat "$FAKE_CURL_SEEN")"
  if [[ -f "${FAKE_SENTRY_STATUS_FILE:-}" ]]; then
    status="$(cat "$FAKE_SENTRY_STATUS_FILE")"
  fi
  cat <<JSON
{"id":"123","shortId":"MOB-123","title":"Crash title","culprit":"MainActivity","permalink":"https://sentry.example.invalid/issues/123/","status":"$status","substatus":null,"level":"error","platform":"android","project":{"id":"7","slug":"mobile","name":"Mobile"},"count":"4","userCount":2,"firstSeen":"2026-08-01T00:00:00Z","lastSeen":"2026-08-11T00:00:00Z","hasSeen":$seen,"assignedTo":{"email":"secret@example.invalid"},"metadata":{"value":"secret-context"}}
JSON
}

case "$url" in
  */api/0/organizations/acme/)
    printf '%s\n' '{"id":"1","slug":"acme","name":"Acme"}'
    ;;
  */api/0/organizations/acme/issues/123/events/latest/)
    cat <<'JSON'
{"eventID":"event-1","groupID":"123","platform":"kotlin","dateCreated":"2026-08-11T00:00:00Z","release":"mobile@4.30.0","environment":"production","exception":{"values":[{"type":"IllegalStateException","value":"bad state","mechanism":{"type":"generic","handled":false,"data":{"secret":"no"}},"stacktrace":{"frames":[{"filename":"lib.kt","function":"library","inApp":false,"vars":{"secret":"no"}},{"filename":"Main.kt","function":"main","module":"app","absPath":"/src/Main.kt","lineNo":42,"colNo":7,"inApp":true},{"filename":"Screen.kt","function":"screen","module":"app","lineNo":50,"colNo":3,"inApp":true}]}}]}}
JSON
    ;;
  */api/0/organizations/acme/issues/123/)
    issue_json
    ;;
  */api/0/organizations/acme/issues/?*)
    cat <<'JSON'
[{"id":"123","shortId":"MOB-123","title":"Seen or unseen","status":"unresolved","hasSeen":false,"project":{"slug":"mobile"},"metadata":{"value":"do-not-emit"}},
 {"id":"124","shortId":"MOB-124","title":"Already seen","status":"unresolved","hasSeen":true,"project":{"slug":"mobile"},"user":{"email":"secret@example.invalid"}}]
JSON
    ;;
  *)
    echo "unexpected fake curl URL: $url" >&2
    exit 1
    ;;
esac
EOF_CURL
chmod +x "$fake_bin/curl"

cat >"$fake_bin/op" <<'EOF_OP'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_OP_LOG"
[[ "${FAKE_OP_FAIL:-false}" != true ]]
printf '%s\n' "op-token"
EOF_OP
chmod +x "$fake_bin/op"

cat >"$fake_bin/sentry-cli" <<'EOF_CLI'
#!/usr/bin/env bash
set -euo pipefail
: "${FAKE_CLI_LOG:?}"
printf 'args=%s\n' "$*" >>"$FAKE_CLI_LOG"
printf 'url=%s\norg=%s\nproject=%s\ntoken=%s\n' \
  "${SENTRY_URL:-}" "${SENTRY_ORG:-}" "${SENTRY_PROJECT:-}" \
  "${SENTRY_AUTH_TOKEN:-}" >>"$FAKE_CLI_LOG"
case "$2" in
  resolve) printf 'resolved\n' >"$FAKE_SENTRY_STATUS_FILE" ;;
  mute) printf 'ignored\n' >"$FAKE_SENTRY_STATUS_FILE" ;;
  unresolve) printf 'unresolved\n' >"$FAKE_SENTRY_STATUS_FILE" ;;
  *) echo "unexpected sentry-cli action" >&2; exit 1 ;;
esac
EOF_CLI
chmod +x "$fake_bin/sentry-cli"

export PATH="$fake_bin:$PATH"
export HOME="$home"
export FAKE_CURL_LOG="$tmp/curl.log"
export FAKE_CURL_STATE="$tmp/curl.state"
export FAKE_CURL_SEEN="$tmp/seen"
export FAKE_OP_LOG="$tmp/op.log"
export FAKE_CLI_LOG="$tmp/cli.log"
export FAKE_SENTRY_STATUS_FILE="$tmp/status"

(
  cd "$project"
  "$adapter" --self-test >"$tmp/self-test.json"
)
jq -e '
  .status == "ok"
  and .base_url == "https://sentry.example.invalid"
  and .organization == "acme"
  and .project == "mobile"
  and .credential_namespace == "ACME"
  and .read_only == false
' "$tmp/self-test.json" >/dev/null
test ! -s "$tmp/curl.log"

cp "$project/.kent/workflow-profile.toml" "$tmp/safe-profile.toml"
printf '%s\n' 'auth_token = "must-not-be-tracked"' \
  >>"$project/.kent/workflow-profile.toml"
if (
  cd "$project"
  "$adapter" --self-test
) >/dev/null 2>"$tmp/tracked-config.err"; then
  echo "tracked Sentry credential configuration unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'unsupported integrations.sentry keys: auth_token' \
  "$tmp/tracked-config.err" >/dev/null
cp "$tmp/safe-profile.toml" "$project/.kent/workflow-profile.toml"

(
  cd "$project"
  KENT_SENTRY_AUTH_TOKEN="kent-token" \
    ACME_SENTRY_AUTH_TOKEN="namespace-token" \
    SENTRY_AUTH_TOKEN="global-token" \
    "$adapter" candidates >"$tmp/kent-priority.json"
)
grep -F 'Bearer kent-token' "$FAKE_CURL_LOG" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" candidates --query 'is:unresolved level:error' --limit 10 \
      >"$tmp/candidates.json"
)
jq -e '
  length == 1
  and .[0].id == "123"
  and .[0].has_seen == false
  and .[0].metadata == null
  and .[0].user == null
' "$tmp/candidates.json" >/dev/null
grep -F 'query=is%3Aunresolved%20level%3Aerror' "$FAKE_CURL_LOG" >/dev/null
grep -F 'limit=10' "$FAKE_CURL_LOG" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" list >"$tmp/all.json"
)
jq -e 'length == 2 and .[1].id == "124"' "$tmp/all.json" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" issue \
      "https://sentry.example.invalid/organizations/acme/issues/123/" \
      >"$tmp/issue.json"
)
jq -e '
  .id == "123"
  and .short_id == "MOB-123"
  and .has_seen == false
  and .metadata == null
' "$tmp/issue.json" >/dev/null
if grep -E 'secret|assignedTo|metadata|\"user\"' "$tmp/issue.json" >/dev/null; then
  echo "issue output leaked a sensitive payload" >&2
  exit 1
fi

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" issue \
      "https://acme.sentry.example.invalid/issues/123/" \
      >"$tmp/org-subdomain-issue.json"
)
jq -e '.id == "123"' "$tmp/org-subdomain-issue.json" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" latest-event 123 --frames 1 >"$tmp/event.json"
)
jq -e '
  .event_id == "event-1"
  and .release == "mobile@4.30.0"
  and .environment == "production"
  and .exceptions[0].type == "IllegalStateException"
  and .exceptions[0].mechanism.type == "generic"
  and (.frames | length) == 1
  and .frames[0].function == "screen"
  and .frames[0].in_app == true
  and (.vars == null)
' "$tmp/event.json" >/dev/null
if grep -E 'secret|vars|context|request|breadcrumb' "$tmp/event.json" >/dev/null; then
  echo "latest-event output leaked a sensitive payload" >&2
  exit 1
fi
grep -F '/api/0/organizations/acme/issues/123/events/latest/' "$FAKE_CURL_LOG" >/dev/null

(
  cd "$project"
  env -u ACME_SENTRY_AUTH_TOKEN \
    ACME_SENTRY_AUTH_TOKEN_OP_REF="op://Private/Sentry/token" \
    "$adapter" --check-auth >"$tmp/auth.json"
)
jq -e '.status == "ok" and .authenticated == true and .organization.slug == "acme"' \
  "$tmp/auth.json" >/dev/null
grep -Fx 'read op://Private/Sentry/token' "$tmp/op.log" >/dev/null

mkdir -p "$home/.kent/credentials/sentry"
printf '%s\n' 'op://Private/Sentry/local-token' >"$home/.kent/credentials/sentry/acme.opref"
chmod 644 "$home/.kent/credentials/sentry/acme.opref"
if (
  cd "$project"
  env -u ACME_SENTRY_AUTH_TOKEN -u ACME_SENTRY_AUTH_TOKEN_OP_REF \
    "$adapter" --check-auth
) >/dev/null 2>"$tmp/insecure-ref.err"; then
  echo "insecure local Sentry credential reference unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'must use mode 0600' "$tmp/insecure-ref.err" >/dev/null
chmod 600 "$home/.kent/credentials/sentry/acme.opref"
(
  cd "$project"
  env -u ACME_SENTRY_AUTH_TOKEN -u ACME_SENTRY_AUTH_TOKEN_OP_REF \
    "$adapter" --check-auth >"$tmp/local-auth.json"
)
jq -e '.authenticated == true' "$tmp/local-auth.json" >/dev/null
grep -Fx 'read op://Private/Sentry/local-token' "$tmp/op.log" >/dev/null

rm -f "$tmp/curl.log" "$tmp/cli.log"
(
  cd "$project"
  "$adapter" mark-seen 123 --dry-run >"$tmp/dry.json"
)
jq -e '.dry_run == true and .issue_id == "123"' "$tmp/dry.json" >/dev/null
test ! -s "$tmp/curl.log"
test ! -s "$tmp/cli.log"

if (
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" "$adapter" resolve 123
) >/dev/null 2>"$tmp/no-approval.err"; then
  echo "resolve unexpectedly succeeded without --allow-mutate" >&2
  exit 1
fi
grep -F 'requires --allow-mutate' "$tmp/no-approval.err" >/dev/null

printf 'unresolved\n' >"$FAKE_SENTRY_STATUS_FILE"
(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" resolve 123 --allow-mutate >"$tmp/resolved.json"
)
jq -e '.action == "resolve" and .verified == true and .status == "resolved"' \
  "$tmp/resolved.json" >/dev/null
grep -Fx 'args=issues resolve --id 123 --org acme --project mobile --auth-token direct-token' \
  "$tmp/cli.log" >/dev/null && {
    echo "Sentry auth token leaked into sentry-cli argv" >&2
    exit 1
  }
grep -Fx 'args=issues resolve --id 123 --org acme --project mobile' \
  "$tmp/cli.log" >/dev/null
grep -Fx 'url=https://sentry.example.invalid' "$tmp/cli.log" >/dev/null
grep -Fx 'org=acme' "$tmp/cli.log" >/dev/null
grep -Fx 'project=mobile' "$tmp/cli.log" >/dev/null
grep -Fx 'token=direct-token' "$tmp/cli.log" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" resolve 123 --next-release --dry-run >"$tmp/next-release.json"
)
jq -e '.dry_run == true and .next_release == true' \
  "$tmp/next-release.json" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" mark-seen 123 --allow-mutate >"$tmp/seen-result.json"
)
jq -e '.action == "mark-seen" and .verified == true and .has_seen == true' \
  "$tmp/seen-result.json" >/dev/null
grep -F 'CALL PUT ' "$FAKE_CURL_STATE" >/dev/null
grep -F '/api/0/organizations/acme/issues/123/' "$FAKE_CURL_LOG" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" mark-unseen 123 --allow-mutate >"$tmp/unseen-result.json"
)
jq -e '.action == "mark-unseen" and .verified == true and .has_seen == false' \
  "$tmp/unseen-result.json" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" mute 123 --allow-mutate >"$tmp/muted.json"
)
jq -e '.action == "mute" and .verified == true and .status == "ignored"' \
  "$tmp/muted.json" >/dev/null
grep -F 'args=issues mute --id 123 --org acme --project mobile' \
  "$tmp/cli.log" >/dev/null

(
  cd "$project"
  ACME_SENTRY_AUTH_TOKEN="direct-token" \
    "$adapter" unresolve 123 --allow-mutate >"$tmp/unresolved.json"
)
jq -e '.action == "unresolve" and .verified == true and .status == "unresolved"' \
  "$tmp/unresolved.json" >/dev/null

if (
  cd "$project"
  env -u ACME_SENTRY_AUTH_TOKEN \
    FAKE_OP_FAIL=true "$adapter" --check-auth
) >/dev/null 2>"$tmp/credential.err"; then
  echo "credential resolution failure unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'unable to resolve Sentry auth token' "$tmp/credential.err" >/dev/null

if (
  cd "$project"
  "$adapter" resolve all --dry-run --allow-mutate
) >/dev/null 2>"$tmp/bulk.err"; then
  echo "bulk mutation unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'numeric Sentry issue ID' "$tmp/bulk.err" >/dev/null

if (
  cd "$project"
  "$adapter" resolve 'https://other.invalid/issues/123/' --dry-run
) >/dev/null 2>"$tmp/foreign-url.err"; then
  echo "foreign Sentry issue URL unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'configured Sentry tenant' "$tmp/foreign-url.err" >/dev/null

echo "Sentry adapter tests passed"
