#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adapter="$repo_root/templates/project/jira-api.sh"
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
case "$url" in
  */rest/api/3/myself)
    cat <<'JSON'
{"accountId":"account-1","displayName":"Test User","active":true}
JSON
    ;;
  */rest/api/3/issue/ABC-123*)
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
    "fixVersions": []
  }
}
JSON
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
export FAKE_OP_LOG="$tmp/op.log"

(
  cd "$tmp/project"
  "$adapter" --self-test >"$tmp/self-test.json"
)
jq -e '
  .status == "ok"
  and .read_only == true
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
' "$tmp/issue.json" >/dev/null
grep -q 'direct-user@example.invalid:direct-token' "$tmp/curl.log"

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

echo "Jira adapter tests passed"
