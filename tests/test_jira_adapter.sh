#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter="$repo_root/templates/project/jira-api.sh"
real_jq="$(command -v jq)"
real_path="$PATH"
tmp="$(mktemp -d)"
trap 'rm -r "$tmp"' EXIT

mkdir -p "$tmp/project/.kent" "$tmp/bin"
git -C "$tmp/project" init -q
cat >"$tmp/project/.kent/workflow-profile.toml" <<'EOF'
[integrations.jira]
base_url = "https://acme.atlassian.net"
credential_namespace = "ACME"
op_vault = "Engineering"
op_item = "Acme Jira"
EOF

cat >"$tmp/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" >"$FAKE_CURL_LOG"
url="${*: -1}"
method=""
for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == "--request" ]]; then
    next=$((index + 1))
    method="${!next}"
    break
  fi
done
case "$method:$url" in
  GET:*|POST:*/rest/api/3/search/jql)
    expected_retry=2
    ;;
  *)
    expected_retry=0
    ;;
esac
actual_retry=""
retry_all=false
for ((index = 1; index <= $#; index++)); do
  case "${!index}" in
    --retry)
      next=$((index + 1))
      actual_retry="${!next}"
      ;;
    --retry-all-errors) retry_all=true ;;
  esac
done
[[ "$actual_retry" == "$expected_retry" ]] || {
  echo "unexpected retry policy for $method $url: $actual_retry" >&2
  exit 90
}
[[ "$retry_all" == "$([[ "$expected_retry" == 2 ]] && echo true || echo false)" ]] ||
  exit 91
printf 'METHOD=%s URL=%s\n' "$method" "$url" >>"${FAKE_CURL_REQUEST_LOG:-$FAKE_CURL_LOG}"
case "$url" in
  */rest/api/3/myself)
    cat <<'JSON'
{"accountId":"account-1","displayName":"Test User","active":true}
JSON
    ;;
  */rest/api/3/issue/ABC-123/transitions*)
    cat <<'JSON'
{"transitions":[{"id":"31","name":"Start progress","to":{"id":"3","name":"In Progress"},"hasScreen":false,"fields":{}}]}
JSON
    ;;
  */rest/api/3/issue/ABC-123/comment*)
    cat <<'JSON'
{"id":"comment-1","self":"https://acme.atlassian.net/rest/api/3/issue/ABC-123/comment/comment-1","created":"2026-08-11T10:00:00.000+0000","author":{"displayName":"Test User"}}
JSON
    ;;
  */rest/api/3/issue)
    cat <<'JSON'
{"id":"10001","key":"ABC-124","self":"https://acme.atlassian.net/rest/api/3/issue/ABC-124"}
JSON
    ;;
  */rest/api/3/issue/ABC-123*)
    if [[ "$method" == "PUT" ]]; then
      printf '{}\n'
    else
    cat <<'JSON'
{
  "key": "ABC-123",
  "fields": {
    "summary": "Generate SDK",
    "status": {"name": "Ready for dev"},
    "issuetype": {"name": "Task"},
    "assignee": {"accountId": "account-1", "displayName": "Test User"},
    "parent": null,
    "description": {
      "type": "doc",
      "content": [
        {
          "type": "paragraph",
          "content": [
            {"type": "text", "text": "Use https://example.invalid/spec"}
          ]
        }
      ]
    },
    "comment": null,
    "labels": ["sdk"],
    "components": [{"id": "component-1", "name": "Mobile"}],
    "fixVersions": [],
    "issuelinks": [
      {
        "id": "link-1",
        "type": {
          "id": "10001",
          "name": "Cloners",
          "inward": "is cloned by",
          "outward": "clones"
        },
        "inwardIssue": {
          "key": "IOS-456",
          "fields": {
            "summary": "[iOS] Generate SDK",
            "status": {"name": "Done"},
            "issuetype": {"name": "Task"}
          }
        }
      },
      {
        "id": "link-2",
        "type": {
          "id": "10003",
          "name": "Relates",
          "inward": "relates to",
          "outward": "relates to"
        },
        "outwardIssue": {
          "key": "WEB-789",
          "fields": {
            "summary": "[Web] Generate SDK",
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"}
          }
        }
      }
    ]
  }
}
JSON
    fi
    ;;
  *)
    echo "unexpected fake curl URL: $url" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$tmp/bin/curl"

cat >"$tmp/bin/op" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_OP_LOG"
if [[ "${FAKE_OP_FAIL:-false}" == "true" ]]; then
  exit 1
fi
case "$2" in
  */email) printf '%s\n' "op-user@example.invalid" ;;
  */token) printf '%s\n' "op-token" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/bin/op"

export PATH="$tmp/bin:$PATH"
export FAKE_CURL_LOG="$tmp/curl.log"
export FAKE_CURL_REQUEST_LOG="$tmp/curl-requests.log"
export FAKE_OP_LOG="$tmp/op.log"

(
  cd "$tmp/project"
  "$adapter" --self-test >"$tmp/self-test.json"
)
jq -e '
  .status == "ok"
  and .read_only == false
  and .base_url == "https://acme.atlassian.net"
  and .credential_namespace == "ACME"
' "$tmp/self-test.json" >/dev/null

(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" \
    ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" issue \
      "https://acme.atlassian.net/browse/ABC-123" >"$tmp/issue.json"
)
jq -e '
  .key == "ABC-123"
  and .summary == "Generate SDK"
  and .description_text == "Use https://example.invalid/spec"
  and (.extracted_urls | index("https://example.invalid/spec") != null)
  and .components == [{"id":"component-1","name":"Mobile"}]
  and .issue_links[0].direction == "inward"
  and .issue_links[0].relationship == "is cloned by"
  and .issue_links[0].issue.key == "IOS-456"
  and .issue_links[1].direction == "outward"
  and .issue_links[1].relationship == "relates to"
  and .issue_links[1].issue.key == "WEB-789"
' "$tmp/issue.json" >/dev/null
grep -q 'direct-user@example.invalid:direct-token' "$tmp/curl.log"
grep -q 'issuelinks' "$tmp/curl.log"

(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" \
    ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" relations ABC-123 >"$tmp/relations.json"
)
jq -e '
  .key == "ABC-123"
  and (.issue_links | length) == 2
  and [.issue_links[].issue.key] == ["IOS-456", "WEB-789"]
' "$tmp/relations.json" >/dev/null

(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" \
    ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" links ABC-123 >"$tmp/links.json"
)
jq -e '
  .key == "ABC-123"
  and .extracted_urls == ["https://example.invalid/spec"]
' "$tmp/links.json" >/dev/null

(
  cd "$tmp/project"
  env \
    -u ACME_JIRA_EMAIL \
    -u ACME_JIRA_API_TOKEN \
    "$adapter" --check-auth >"$tmp/auth.json"
)
jq -e '
  .account_id == "account-1"
  and .display_name == "Test User"
  and .active == true
' "$tmp/auth.json" >/dev/null
grep -q 'read op://Engineering/Acme Jira/email' "$tmp/op.log"
grep -q 'read op://Engineering/Acme Jira/token' "$tmp/op.log"

rm -f "$tmp/curl.log"
if (
  cd "$tmp/project"
  env \
    -u ACME_JIRA_EMAIL \
    -u ACME_JIRA_API_TOKEN \
    FAKE_OP_FAIL=true \
    "$adapter" --check-auth
) >/dev/null 2>&1; then
  echo "Jira adapter unexpectedly ignored a credential resolution failure" >&2
  exit 1
fi
if [[ -e "$tmp/curl.log" ]]; then
  echo "Jira adapter called HTTP after credential resolution failed" >&2
  exit 1
fi

if (
  cd "$tmp/project"
  "$adapter" create-issue ABC
) >/dev/null 2>&1; then
  echo "Jira adapter unexpectedly accepted a mutation command" >&2
  exit 1
fi


# Dry-run rendering is local: it must not resolve credentials or call curl.
rm -f "$tmp/curl.log" "$tmp/curl-requests.log" "$tmp/op.log"
(
  cd "$tmp/project"
  env -u ACME_JIRA_EMAIL -u ACME_JIRA_API_TOKEN \
    "$adapter" create-issue ABC --type Task --summary "Create SDK" \
      --description $'Line one\nLine two' --label Android --fix-version 123 \
      --assignee me --parent ABC-123 --dry-run >"$tmp/create-dry-run.json"
)
jq -e '
  .dryRun == true
  and .contentLanguage == "en"
  and .payload.fields.project.key == "ABC"
  and .payload.fields.issuetype.name == "Task"
  and .payload.fields.summary == "Create SDK"
  and .payload.fields.description.content[1].content[0].text == "Line two"
  and .payload.fields.labels == ["Android"]
  and .payload.fields.fixVersions == [{id: "123"}]
  and .payload.fields.assignee.accountId == "currentUser()"
  and .payload.fields.parent.key == "ABC-123"
' "$tmp/create-dry-run.json" >/dev/null
[[ ! -e "$tmp/curl-requests.log" ]] || { echo "dry-run called curl" >&2; exit 1; }
[[ ! -e "$tmp/op.log" ]] || { echo "dry-run resolved credentials" >&2; exit 1; }

if (
  cd "$tmp/project"
  env -u ACME_JIRA_EMAIL -u ACME_JIRA_API_TOKEN \
    "$adapter" create-issue ABC --type Task --summary "Русский" --allow-mutate
) >/dev/null 2>&1; then
  echo "English guard unexpectedly accepted Cyrillic before auth" >&2
  exit 1
fi
[[ ! -e "$tmp/curl-requests.log" ]] || { echo "English guard called curl" >&2; exit 1; }
[[ ! -e "$tmp/op.log" ]] || { echo "English guard resolved credentials" >&2; exit 1; }

if (cd "$tmp/project" && env -u ACME_JIRA_EMAIL -u ACME_JIRA_API_TOKEN \
  "$adapter" create-issue ABC --type Task --summary "Ёж" --allow-mutate \
  >/dev/null 2>&1); then
  echo "English guard unexpectedly accepted Ёж before auth" >&2
  exit 1
fi
[[ ! -e "$tmp/curl-requests.log" && ! -e "$tmp/op.log" ]] ||
  { echo "Ёж guard reached curl or credentials" >&2; exit 1; }

(
  cd "$tmp/project" &&
    env -u ACME_JIRA_EMAIL -u ACME_JIRA_API_TOKEN "$adapter" create-issue ABC \
      --type Task --summary "Русский" --allow-non-english --dry-run
) >"$tmp/russian-dry-run.json"
jq -e '.dryRun == true and .contentLanguage == "explicit-non-english"
  and .payload.fields.summary == "Русский"' "$tmp/russian-dry-run.json" >/dev/null
[[ ! -e "$tmp/curl-requests.log" && ! -e "$tmp/op.log" ]] ||
  { echo "non-English dry-run reached curl or credentials" >&2; exit 1; }

cat >"$tmp/bin/jq" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for argument in "$@"; do
  [[ "$argument" == *'test("[\u0410-\u042F\u0430-\u044F\u0401\u0451]")'* ]] || continue
  printf 'language-filter-error\n' >>"$JQ_SHIM_LOG"; exit 91
done
exec "$REAL_JQ" "$@"
EOF
chmod +x "$tmp/bin/jq"
export REAL_JQ="$real_jq" JQ_SHIM_LOG="$tmp/jq-shim.log"
if (cd "$tmp/project" && env -u ACME_JIRA_EMAIL -u ACME_JIRA_API_TOKEN \
  "$adapter" create-issue ABC --type Task --summary "Русский" --allow-mutate \
  >/dev/null 2>&1); then
  echo "Jira adapter unexpectedly ignored a jq language-filter failure" >&2
  exit 1
fi
grep -q '^language-filter-error$' "$tmp/jq-shim.log" &&
  [[ ! -e "$tmp/curl-requests.log" && ! -e "$tmp/op.log" ]] ||
  { echo "jq failure reached curl or credentials" >&2; exit 1; }
rm "$tmp/bin/jq"

if (
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" create-issue ABC --type Task --summary "Create SDK"
) >/dev/null 2>&1; then
  echo "create-issue approval gate was bypassed" >&2
  exit 1
fi
[[ ! -e "$tmp/curl-requests.log" ]] || { echo "approval gate called curl" >&2; exit 1; }

rm -f "$tmp/curl.log" "$tmp/curl-requests.log"
(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" create-issue ABC --type Task --summary "Create SDK" --allow-mutate >"$tmp/create.json"
)
jq -e '.key == "ABC-124" and .contentLanguage == "en"' "$tmp/create.json" >/dev/null
grep -q 'METHOD=POST URL=https://acme.atlassian.net/rest/api/3/issue' "$tmp/curl-requests.log"
grep -q '"summary": "Create SDK"' "$tmp/curl.log"

rm -f "$tmp/curl.log" "$tmp/curl-requests.log"
(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" edit-issue ABC-123 --summary "Edited SDK" --clear-labels \
      --clear-fix-versions --clear-parent --allow-mutate >"$tmp/edit.json"
)
jq -e '.key == "ABC-123" and .updated == true and .contentLanguage == "en"' "$tmp/edit.json" >/dev/null
grep -q 'METHOD=PUT URL=https://acme.atlassian.net/rest/api/3/issue/ABC-123' "$tmp/curl-requests.log"
grep -q '"labels": \[\]' "$tmp/curl.log"
grep -q '"fixVersions": \[\]' "$tmp/curl.log"
grep -q '"parent": null' "$tmp/curl.log"

rm -f "$tmp/curl.log" "$tmp/curl-requests.log"
(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" comment-issue ABC-123 --body "Review ready" --allow-mutate >"$tmp/comment.json"
)
jq -e '.issueKey == "ABC-123" and .id == "comment-1" and .contentLanguage == "en"' "$tmp/comment.json" >/dev/null
grep -q 'METHOD=POST URL=https://acme.atlassian.net/rest/api/3/issue/ABC-123/comment' "$tmp/curl-requests.log"
grep -q '"body": {' "$tmp/curl.log"

rm -f "$tmp/curl.log" "$tmp/curl-requests.log"
(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" transition-issue ABC-123 --to "In Progress" --dry-run >"$tmp/transition-dry-run.json"
)
jq -e '.dryRun == true and .transition.id == "31" and .transition.to == "In Progress"' \
  "$tmp/transition-dry-run.json" >/dev/null
if grep -q 'METHOD=POST URL=https://acme.atlassian.net/rest/api/3/issue/ABC-123/transitions' \
  "$tmp/curl-requests.log"; then
  echo "transition dry-run mutated Jira" >&2
  exit 1
fi
grep -q 'METHOD=GET URL=https://acme.atlassian.net/rest/api/3/issue/ABC-123/transitions' \
  "$tmp/curl-requests.log"

rm -f "$tmp/curl.log" "$tmp/curl-requests.log"
(
  cd "$tmp/project"
  ACME_JIRA_EMAIL="direct-user@example.invalid" ACME_JIRA_API_TOKEN="direct-token" \
    "$adapter" transition-issue ABC-123 --to 31 --allow-mutate >"$tmp/transition.json"
)
jq -e '.key == "ABC-123" and .transitioned == true and .transition.id == "31"' \
  "$tmp/transition.json" >/dev/null
grep -q 'METHOD=POST URL=https://acme.atlassian.net/rest/api/3/issue/ABC-123/transitions' \
  "$tmp/curl-requests.log"

# Exercise the real curl retry behavior against a loopback-only 503 responder.
python3 - "$adapter" "$tmp/project" "$real_path" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import subprocess
import sys
import threading

adapter, project, real_path = sys.argv[1:]
requests = []
allow_transition_lookup = False


class Handler(BaseHTTPRequestHandler):
    def handle_request(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        requests.append((self.command, self.path))
        lookup = (allow_transition_lookup and self.command == "GET"
                  and "/transitions" in self.path)
        body = ({"transitions": [{"id": "31", "name": "Start",
                                "to": {"name": "In Progress"}}]}
                if lookup else {"error": "fixture unavailable"})
        raw = json.dumps(body).encode()
        self.send_response(200 if lookup else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = handle_request

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
environment = {
    **os.environ,
    "PATH": real_path,
    "KENT_JIRA_URL": f"http://127.0.0.1:{server.server_port}",
    "KENT_JIRA_EMAIL": "fixture@example.invalid",
    "KENT_JIRA_API_TOKEN": "fixture-token",
    "NO_PROXY": "*",
    "no_proxy": "*",
}
cases = [
    (["create-issue", "ABC", "--type", "Task", "--summary", "Retry fixture",
      "--allow-mutate"], "POST", "/rest/api/3/issue", 1),
    (["edit-issue", "ABC-123", "--summary", "Retry fixture", "--allow-mutate"],
     "PUT", "/rest/api/3/issue/ABC-123", 1),
    (["comment-issue", "ABC-123", "--body", "Retry fixture", "--allow-mutate"],
     "POST", "/rest/api/3/issue/ABC-123/comment", 1),
    (["transition-issue", "ABC-123", "--to", "31", "--allow-mutate"],
     "POST", "/rest/api/3/issue/ABC-123/transitions", 1),
    (["issue", "ABC-123"], "GET", "/rest/api/3/issue/ABC-123", 3),
    (["transitions", "ABC-123"], "GET", "/rest/api/3/issue/ABC-123/transitions", 3),
    (["search", "project = ABC"], "POST", "/rest/api/3/search/jql", 3),
]
try:
    for arguments, method, path, expected in cases:
        requests.clear()
        allow_transition_lookup = arguments[0] == "transition-issue"
        result = subprocess.run(
            [adapter, *arguments], cwd=project, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        actual = sum(verb == method and target.split("?")[0] == path
                     for verb, target in requests)
        assert result.returncode != 0, (arguments[0], "503 reported as success")
        assert actual == expected, (arguments[0], actual, expected)
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)
print("Jira HTTP-503 retry matrix: 7 cases passed")
PY

echo "Jira adapter tests passed"
