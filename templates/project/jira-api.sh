#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  jira-api.sh key <JIRA_URL_OR_KEY>
  jira-api.sh --self-test
  jira-api.sh --check-auth
  jira-api.sh issue <KEY_OR_URL>
  jira-api.sh links <KEY_OR_URL>
  jira-api.sh relations <KEY_OR_URL>
  jira-api.sh comments <KEY_OR_URL>
  jira-api.sh search <JQL> [MAX_RESULTS]
  jira-api.sh board <BOARD_URL_OR_ID>
  jira-api.sh board-issues <BOARD_URL_OR_ID> [--status <STATUS>] [--assignee me|currentUser] [--limit <1..100>]

The adapter is read-only. Credentials are resolved in this order:
  1. KENT_JIRA_* environment variables.
  2. <CREDENTIAL_NAMESPACE>_JIRA_* environment variables.
  3. JIRA_* environment variables.
  4. Matching *_OP_REF variables through 1Password.
  5. The non-secret 1Password vault/item pointers declared by the project.

Optional project profile:
  [integrations.jira]
  base_url = "https://example.atlassian.net"
  credential_namespace = "EXAMPLE"
  op_vault = "Private"
  op_item = "Example Jira API Token"
EOF
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "jira-api: missing command: $command_name" >&2
    return 127
  fi
}

select_python() {
  local candidate
  if [[ -n "${KENT_ENGINEERING_KIT_PYTHON:-}" ]]; then
    candidate="$KENT_ENGINEERING_KIT_PYTHON"
    if [[ -x "$candidate" ]] &&
      "$candidate" -I -S -c \
        'import sys, tomllib; raise SystemExit(sys.version_info < (3, 11))' \
        >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return
    fi
    echo "jira-api: KENT_ENGINEERING_KIT_PYTHON must be Python 3.11 or newer" >&2
    return 1
  fi

  for candidate in \
    "$(command -v python3.14 2>/dev/null || true)" \
    "$(command -v python3.13 2>/dev/null || true)" \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$(command -v python3.11 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /home/linuxbrew/.linuxbrew/bin/python3 \
    /usr/bin/python3; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if "$candidate" -I -S -c \
      'import sys, tomllib; raise SystemExit(sys.version_info < (3, 11))' \
      >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  echo "jira-api: Python 3.11 or newer is required to read the project profile" >&2
  return 1
}

project_profile_path() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  printf '%s/.kent/workflow-profile.toml\n' "$root"
}

profile_jira_json() {
  local profile python_runtime
  profile="$(project_profile_path)"
  if [[ ! -f "$profile" ]]; then
    printf '{}\n'
    return
  fi

  python_runtime="$(select_python)"
  "$python_runtime" -I -S - "$profile" <<'PY'
import json
from pathlib import Path
import sys
import tomllib

profile = tomllib.loads(Path(sys.argv[1]).read_text())
jira = profile.get("integrations", {}).get("jira", {})
if not isinstance(jira, dict):
    raise SystemExit("integrations.jira must be a TOML table")

allowed = {
    key: value
    for key, value in jira.items()
    if key in {"base_url", "credential_namespace", "op_vault", "op_item"}
    and isinstance(value, str)
    and value.strip()
}
print(json.dumps(allowed))
PY
}

profile_value() {
  local key="$1"
  jq -r --arg key "$key" '.[$key] // empty' <<<"$PROFILE_JIRA_JSON"
}

indirect_value() {
  local variable_name="$1"
  printf '%s' "${!variable_name-}"
}

credential_value() {
  local suffix="$1"
  local variable_name value
  local candidates=("KENT_JIRA_${suffix}")
  if [[ -n "$CREDENTIAL_NAMESPACE" ]]; then
    candidates+=("${CREDENTIAL_NAMESPACE}_JIRA_${suffix}")
  fi
  candidates+=("JIRA_${suffix}")

  for variable_name in "${candidates[@]}"; do
    value="$(indirect_value "$variable_name")"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
  done
}

credential_ref() {
  local suffix="$1"
  local variable_name value
  local candidates=("KENT_JIRA_${suffix}_OP_REF")
  if [[ -n "$CREDENTIAL_NAMESPACE" ]]; then
    candidates+=("${CREDENTIAL_NAMESPACE}_JIRA_${suffix}_OP_REF")
  fi
  candidates+=("JIRA_${suffix}_OP_REF")

  for variable_name in "${candidates[@]}"; do
    value="$(indirect_value "$variable_name")"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
  done
}

read_credential() {
  local suffix="$1"
  local field="$2"
  local value ref

  value="$(credential_value "$suffix")"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return
  fi

  ref="$(credential_ref "$suffix")"
  if [[ -z "$ref" && -n "$OP_VAULT" && -n "$OP_ITEM" ]]; then
    ref="op://${OP_VAULT}/${OP_ITEM}/${field}"
  fi
  if [[ -z "$ref" ]]; then
    echo "jira-api: missing credential ${suffix}" >&2
    return 1
  fi

  require_command op
  if ! value="$(op read "$ref")"; then
    echo "jira-api: unable to resolve credential ${suffix} from 1Password" >&2
    return 1
  fi
  if [[ -z "$value" ]]; then
    echo "jira-api: 1Password returned an empty credential ${suffix}" >&2
    return 1
  fi
  printf '%s' "$value"
}

normalize_base_url() {
  local value="$1"
  printf '%s' "${value%/}"
}

urlencode() {
  jq -rn --arg value "$1" '$value | @uri'
}

issue_key() {
  local value="${1:-}"
  local match
  match="$(grep -Eio '[A-Z][A-Z0-9_]+-[0-9]+' <<<"$value" | head -1 || true)"
  if [[ -z "$match" ]]; then
    echo "jira-api: cannot resolve Jira issue key from: $value" >&2
    return 2
  fi
  tr '[:lower:]' '[:upper:]' <<<"$match"
}

board_id() {
  local value="${1:-}"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return
  fi
  if [[ "$value" =~ /boards/([0-9]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return
  fi
  echo "jira-api: cannot resolve Jira board ID from: $value" >&2
  return 2
}

jira_request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local email token
  [[ -n "$JIRA_BASE_URL" ]] || {
    echo "jira-api: Jira base URL is not configured" >&2
    return 1
  }
  email="$(read_credential EMAIL email)"
  token="$(read_credential API_TOKEN token)"

  local arguments=(
    --silent
    --show-error
    --fail-with-body
    --connect-timeout 15
    --max-time 90
    --retry 2
    --retry-all-errors
    --request "$method"
    --user "${email}:${token}"
    --header "Accept: application/json"
  )
  if [[ -n "$body" ]]; then
    arguments+=(
      --header "Content-Type: application/json"
      --data "$body"
    )
  fi
  curl "${arguments[@]}" "${JIRA_BASE_URL}${path}"
}

normalize_issue_filter() {
  cat <<'JQ'
def adf_text:
  [.. | objects | select(.type? == "text") | .text? | select(type == "string")]
  | join("\n");
def urls:
  [.. | strings | scan("https?://[^[:space:]<>]+")]
  | map(sub("[),.;]+$"; ""))
  | unique;
{
  key: .key,
  url: ($base_url + "/browse/" + .key),
  summary: .fields.summary,
  status: .fields.status.name,
  type: .fields.issuetype.name,
  assignee: (
    .fields.assignee
    | if . == null then null else {
        account_id: .accountId,
        display_name: .displayName
      } end
  ),
  parent: (
    .fields.parent
    | if . == null then null else {
        key: .key,
        summary: .fields.summary
      } end
  ),
  labels: (.fields.labels // []),
  components: [
    .fields.components[]?
    | {id: .id, name: .name}
  ],
  fix_versions: [
    .fields.fixVersions[]?
    | {id: .id, name: .name, released: .released}
  ],
  issue_links: [
    .fields.issuelinks[]? as $link
    | if $link.inwardIssue? then
        {
          id: ($link.id | tostring),
          link_type: {
            id: ($link.type.id | tostring),
            name: $link.type.name,
            inward: $link.type.inward,
            outward: $link.type.outward
          },
          direction: "inward",
          relationship: $link.type.inward,
          issue: {
            key: $link.inwardIssue.key,
            url: ($base_url + "/browse/" + $link.inwardIssue.key),
            summary: ($link.inwardIssue.fields.summary // null),
            status: ($link.inwardIssue.fields.status.name // null),
            type: ($link.inwardIssue.fields.issuetype.name // null)
          }
        }
      elif $link.outwardIssue? then
        {
          id: ($link.id | tostring),
          link_type: {
            id: ($link.type.id | tostring),
            name: $link.type.name,
            inward: $link.type.inward,
            outward: $link.type.outward
          },
          direction: "outward",
          relationship: $link.type.outward,
          issue: {
            key: $link.outwardIssue.key,
            url: ($base_url + "/browse/" + $link.outwardIssue.key),
            summary: ($link.outwardIssue.fields.summary // null),
            status: ($link.outwardIssue.fields.status.name // null),
            type: ($link.outwardIssue.fields.issuetype.name // null)
          }
        }
      else empty end
  ],
  description_text: ((.fields.description // {}) | adf_text),
  extracted_urls: (
    [.fields.description, .fields.comment]
    | urls
  )
}
JQ
}

normalize_issue() {
  local filter
  filter="$(normalize_issue_filter)"
  jq --arg base_url "$JIRA_BASE_URL" "$filter"
}

issue_command() {
  local key fields raw
  key="$(issue_key "$1")"
  fields="summary,status,issuetype,assignee,parent,description,comment,labels,components,fixVersions,issuelinks"
  raw="$(jira_request GET "/rest/api/3/issue/${key}?fields=${fields}")"
  normalize_issue <<<"$raw"
}

comments_command() {
  local key raw
  key="$(issue_key "$1")"
  raw="$(jira_request GET "/rest/api/3/issue/${key}/comment?maxResults=100")"
  jq '
    def adf_text:
      [.. | objects | select(.type? == "text") | .text? | select(type == "string")]
      | join("\n");
    {
      issue_key: $issue_key,
      comments: [
        .comments[]?
        | {
            id: .id,
            author: .author.displayName,
            created: .created,
            updated: .updated,
            body_text: ((.body // {}) | adf_text)
          }
      ]
    }
  ' --arg issue_key "$key" <<<"$raw"
}

search_command() {
  local jql="$1"
  local max_results="${2:-50}"
  if [[ ! "$max_results" =~ ^[0-9]+$ ]] || ((max_results < 1 || max_results > 100)); then
    echo "jira-api: max results must be between 1 and 100" >&2
    return 2
  fi

  local body raw filter
  body="$(
    jq -n \
      --arg jql "$jql" \
      --argjson max_results "$max_results" \
      '{
        jql: $jql,
        maxResults: $max_results,
        fields: [
          "summary",
          "status",
          "issuetype",
          "assignee",
          "parent",
          "description",
          "labels",
          "fixVersions"
        ]
      }'
  )"
  raw="$(jira_request POST "/rest/api/3/search/jql" "$body")"
  filter="$(normalize_issue_filter)"
  jq --arg base_url "$JIRA_BASE_URL" "
    {
      total: (.total // (.issues | length)),
      next_page_token: (.nextPageToken // null),
      issues: [.issues[] | ${filter}]
    }
  " <<<"$raw"
}

board_command() {
  local id raw
  id="$(board_id "$1")"
  raw="$(jira_request GET "/rest/agile/1.0/board/${id}")"
  jq '{
    id: .id,
    name: .name,
    type: .type,
    location: .location
  }' <<<"$raw"
}

board_issues_command() {
  local source="$1"
  shift
  local status=""
  local assignee=""
  local limit=50
  while (($#)); do
    case "$1" in
      --status)
        status="${2:-}"
        shift 2
        ;;
      --assignee)
        assignee="${2:-}"
        shift 2
        ;;
      --limit)
        limit="${2:-}"
        shift 2
        ;;
      *)
        echo "jira-api: unknown board-issues argument: $1" >&2
        return 2
        ;;
    esac
  done
  if [[ ! "$limit" =~ ^[0-9]+$ ]] || ((limit < 1 || limit > 100)); then
    echo "jira-api: limit must be between 1 and 100" >&2
    return 2
  fi
  if [[ -n "$assignee" && "$assignee" != "me" && "$assignee" != "currentUser" ]]; then
    echo "jira-api: assignee must be me or currentUser" >&2
    return 2
  fi

  local id jql=""
  id="$(board_id "$source")"
  if [[ -n "$status" ]]; then
    jql="status = \"${status//\"/\\\"}\""
  fi
  if [[ -n "$assignee" ]]; then
    [[ -n "$jql" ]] && jql+=" AND "
    jql+="assignee = currentUser()"
  fi

  local path fields raw filter
  fields="summary,status,issuetype,assignee,parent,description,labels,fixVersions"
  path="/rest/agile/1.0/board/${id}/issue?maxResults=${limit}&fields=$(urlencode "$fields")"
  if [[ -n "$jql" ]]; then
    path+="&jql=$(urlencode "$jql")"
  fi
  raw="$(jira_request GET "$path")"
  filter="$(normalize_issue_filter)"
  jq --arg base_url "$JIRA_BASE_URL" --argjson board_id "$id" "
    {
      board_id: \$board_id,
      total: (.total // (.issues | length)),
      issues: [.issues[] | ${filter}]
    }
  " <<<"$raw"
}

require_command jq
require_command curl
PROFILE_JIRA_JSON="$(profile_jira_json)"
CREDENTIAL_NAMESPACE="${KENT_JIRA_CREDENTIAL_NAMESPACE:-${JIRA_CREDENTIAL_NAMESPACE:-}}"
if [[ -z "$CREDENTIAL_NAMESPACE" ]]; then
  CREDENTIAL_NAMESPACE="$(profile_value credential_namespace)"
fi
CREDENTIAL_NAMESPACE="$(
  tr '[:lower:]-' '[:upper:]_' <<<"$CREDENTIAL_NAMESPACE"
)"
if [[ -n "$CREDENTIAL_NAMESPACE" && ! "$CREDENTIAL_NAMESPACE" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
  echo "jira-api: invalid credential namespace: $CREDENTIAL_NAMESPACE" >&2
  exit 2
fi
OP_VAULT="$(credential_value OP_VAULT)"
[[ -n "$OP_VAULT" ]] || OP_VAULT="$(profile_value op_vault)"
OP_ITEM="$(credential_value OP_ITEM)"
[[ -n "$OP_ITEM" ]] || OP_ITEM="$(profile_value op_item)"
JIRA_BASE_URL="$(credential_value URL)"
[[ -n "$JIRA_BASE_URL" ]] || JIRA_BASE_URL="$(profile_value base_url)"
JIRA_BASE_URL="$(normalize_base_url "$JIRA_BASE_URL")"

case "${1:-}" in
  --help|-h)
    usage
    ;;
  --self-test)
    jq -n \
      --arg status "ok" \
      --arg base_url "$JIRA_BASE_URL" \
      --arg credential_namespace "$CREDENTIAL_NAMESPACE" \
      --argjson op_available "$(
        if command -v op >/dev/null 2>&1; then echo true; else echo false; fi
      )" \
      '{
        status: $status,
        read_only: true,
        base_url: $base_url,
        credential_namespace: $credential_namespace,
        one_password_available: $op_available
      }'
    ;;
  --check-auth)
    [[ -n "$JIRA_BASE_URL" ]] || {
      echo "jira-api: Jira base URL is not configured" >&2
      exit 1
    }
    jira_request GET "/rest/api/3/myself" |
      jq '{
        account_id: .accountId,
        display_name: .displayName,
        active: .active
      }'
    ;;
  key)
    issue_key "${2:-}"
    ;;
  issue)
    [[ -n "$JIRA_BASE_URL" ]] || {
      echo "jira-api: Jira base URL is not configured" >&2
      exit 1
    }
    issue_command "${2:-}"
    ;;
  links)
    issue_command "${2:-}" | jq '{key, extracted_urls}'
    ;;
  relations)
    issue_command "${2:-}" | jq '{key, issue_links}'
    ;;
  comments)
    comments_command "${2:-}"
    ;;
  search)
    search_command "${2:-}" "${3:-50}"
    ;;
  board)
    board_command "${2:-}"
    ;;
  board-issues)
    shift
    board_issues_command "$@"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
