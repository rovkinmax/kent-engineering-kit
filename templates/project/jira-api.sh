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
  jira-api.sh transitions <KEY_OR_URL>
  jira-api.sh transition-issue <KEY_OR_URL> --to <STATUS_OR_TRANSITION_NAME_OR_ID> \
    [--dry-run] [--allow-mutate]
  jira-api.sh create-issue <PROJECT_KEY> --type <ISSUE_TYPE> --summary <SUMMARY> \
    [--description <TEXT>|--description-file <PATH>|--description-adf-file <PATH>] \
    [--label <LABEL>] [--assignee me|unassigned] \
    [--assignee-account-id <ACCOUNT_ID>] [--fix-version <VERSION_NAME_OR_ID>] [--parent <KEY>] \
    [--allow-non-english] [--dry-run] [--allow-mutate]
  jira-api.sh edit-issue <KEY_OR_URL> \
    [--summary <SUMMARY>] [--description <TEXT>|--description-file <PATH>|--description-adf-file <PATH>] \
    [--label <LABEL>] [--clear-labels] [--assignee me|unassigned] \
    [--assignee-account-id <ACCOUNT_ID>] [--fix-version <VERSION_NAME_OR_ID>] \
    [--clear-fix-versions] [--parent <KEY>] [--clear-parent] \
    [--allow-non-english] [--dry-run] [--allow-mutate]
  jira-api.sh comment-issue <KEY_OR_URL> \
    (--body <TEXT>|--body-file <PATH>|--body-adf-file <PATH>) \
    [--allow-non-english] [--dry-run] [--allow-mutate]

Credentials are resolved in this order:
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
  require_command curl
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

validate_non_empty() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    echo "jira-api: ${name} must not be empty" >&2
    return 2
  fi
}

validate_english_text() {
  local field_name="$1"
  local value="$2"
  local allow_non_english="$3"
  local contains_cyrillic
  if [[ "$allow_non_english" == true || -z "$value" ]]; then
    return
  fi
  if ! contains_cyrillic="$(
    jq -nr --arg value "$value" \
      '$value | test("[\u0410-\u042F\u0430-\u044F\u0401\u0451]")' 2>/dev/null
  )"; then
    echo "jira-api: unable to validate ${field_name} language" >&2
    return 2
  fi
  if [[ "$contains_cyrillic" == true ]]; then
    echo "jira-api: ${field_name} contains Cyrillic; pass --allow-non-english to override" >&2
    return 2
  fi
  if [[ "$contains_cyrillic" != false ]]; then
    echo "jira-api: unable to validate ${field_name} language" >&2
    return 2
  fi
}

validate_english_json() {
  local field_name="$1"
  local json_value="$2"
  local allow_non_english="$3"
  local text
  [[ "$allow_non_english" == true || "$json_value" == null ]] && return
  text="$(jq -r '[.. | strings] | join("\n")' <<<"$json_value")"
  validate_english_text "$field_name" "$text" false
}

content_language() {
  if [[ "$1" == true ]]; then
    printf 'explicit-non-english\n'
  else
    printf 'en\n'
  fi
}

validate_label() {
  local label="$1"
  if [[ ! "$label" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "jira-api: invalid label: $label" >&2
    return 2
  fi
}

validate_fix_version() {
  local value="$1"
  if [[ -z "$value" || "$value" == *$'\n'* ]]; then
    echo "jira-api: fix version must be a non-empty single-line value" >&2
    return 2
  fi
}

json_string_array() {
  if (($# == 0)); then
    printf '[]\n'
  else
    printf '%s\n' "$@" | jq -R . | jq -s .
  fi
}

json_fix_versions() {
  if (($# == 0)); then
    printf '[]\n'
  else
    printf '%s\n' "$@" | jq -R 'if test("^[0-9]+$") then {id: .} else {name: .} end' | jq -s .
  fi
}

build_adf_from_text() {
  jq -n --arg text "$1" '
    {
      type: "doc",
      version: 1,
      content: ($text | split("\n") | map({
        type: "paragraph",
        content: (if . == "" then [] else [{type: "text", text: .}] end)
      }))
    }
  '
}

validate_adf_file() {
  local path="$1"
  [[ -f "$path" ]] || {
    echo "jira-api: missing ADF file: $path" >&2
    return 2
  }
  jq -e '
    type == "object"
    and .type == "doc"
    and .version == 1
    and (.content | type == "array")
  ' "$path" >/dev/null || {
    echo "jira-api: invalid ADF document in: $path" >&2
    return 2
  }
}

build_content_json() {
  local text="$1"
  local text_file="$2"
  local adf_file="$3"
  local source_count=0
  [[ -n "$text" ]] && source_count=$((source_count + 1))
  [[ -n "$text_file" ]] && source_count=$((source_count + 1))
  [[ -n "$adf_file" ]] && source_count=$((source_count + 1))
  if ((source_count > 1)); then
    echo "jira-api: content sources are mutually exclusive" >&2
    return 2
  fi
  if [[ -n "$adf_file" ]]; then
    validate_adf_file "$adf_file" || return
    jq -c . "$adf_file"
  elif [[ -n "$text_file" ]]; then
    [[ -f "$text_file" ]] || {
      echo "jira-api: missing content file: $text_file" >&2
      return 2
    }
    build_adf_from_text "$(<"$text_file")"
  elif [[ -n "$text" ]]; then
    build_adf_from_text "$text"
  else
    printf 'null\n'
  fi
}

validate_assignee() {
  case "$1" in
    ""|me|currentUser|currentUser\(\)|unassigned|null)
      return
      ;;
    *)
      echo "jira-api: assignee must be me, currentUser, or unassigned" >&2
      return 2
      ;;
  esac
}

assignee_json_for_dry_run() {
  case "$1" in
    "") printf 'null\n' ;;
    me|currentUser|currentUser\(\)) jq -n '{accountId: "currentUser()"}' ;;
    unassigned|null) jq -n '{_jiraUnassign: true}' ;;
    account-id) jq -n --arg accountId "$2" '{accountId: $accountId}' ;;
  esac
}

assignee_json_for_network() {
  case "$1" in
    "") printf 'null\n' ;;
    me|currentUser|currentUser\(\)) jira_request GET "/rest/api/3/myself" | jq '{accountId}' ;;
    unassigned|null) jq -n '{_jiraUnassign: true}' ;;
    account-id) jq -n --arg accountId "$2" '{accountId: $accountId}' ;;
  esac
}

build_create_issue_payload() {
  jq -n \
    --arg project "$1" --arg issue_type "$2" --arg summary "$3" --arg parent "$4" \
    --argjson description "$5" --argjson labels "$6" --argjson assignee "$7" --argjson fix_versions "$8" '
    {
      fields: (
        {
          project: {key: $project},
          issuetype: {name: $issue_type},
          summary: $summary
        }
        + (if $description == null then {} else {description: $description} end)
        + (if ($labels | length) == 0 then {} else {labels: $labels} end)
        + (
          if $assignee == null then {}
          elif ($assignee._jiraUnassign // false) then {assignee: null}
          else {assignee: $assignee}
          end
        )
        + (if $parent == "" then {} else {parent: {key: $parent}} end)
        + (if ($fix_versions | length) == 0 then {} else {fixVersions: $fix_versions} end)
      )
    }
  '
}

build_edit_issue_payload() {
  jq -n \
    --arg summary "$1" --arg parent "$2" --argjson clear_parent "$3" \
    --argjson description "$4" --argjson labels "$5" --argjson replace_labels "$6" \
    --argjson assignee "$7" --argjson fix_versions "$8" --argjson replace_fix_versions "$9" '
    {
      fields: (
        {}
        + (if $summary == "" then {} else {summary: $summary} end)
        + (if $description == null then {} else {description: $description} end)
        + (if $replace_labels then {labels: $labels} else {} end)
        + (if $replace_fix_versions then {fixVersions: $fix_versions} else {} end)
        + (if $clear_parent then {parent: null} elif $parent != "" then {parent: {key: $parent}} else {} end)
        + (
          if $assignee == null then {}
          elif ($assignee._jiraUnassign // false) then {assignee: null}
          else {assignee: $assignee}
          end
        )
      )
    }
  '
}

build_comment_payload() {
  jq -n --argjson body "$1" '{body: $body}'
}

require_mutation_approval() {
  [[ "$1" == true ]] || {
    echo "jira-api: ${2} requires exact --allow-mutate or --dry-run" >&2
    return 2
  }
}

create_issue_command() {
  local project="$1"
  shift
  local issue_type="" summary="" description="" description_file="" description_adf_file=""
  local parent="" assignee_mode="" assignee_account_id="" dry_run=false allow_mutate=false
  local allow_non_english=false description_json labels_json assignee_json fix_versions_json payload language
  local -a labels=()
  local -a fix_versions=()

  while (($#)); do
    case "$1" in
      --type|--summary|--description|--description-file|--description-adf-file|--label|--fix-version|\
      --assignee|--assignee-account-id|--parent)
        [[ $# -ge 2 ]] || { echo "jira-api: $1 requires a value" >&2; return 2; }
        case "$1" in
          --type) issue_type="$2" ;;
          --summary) summary="$2" ;;
          --description) description="$2" ;;
          --description-file) description_file="$2" ;;
          --description-adf-file) description_adf_file="$2" ;;
          --label) validate_label "$2" || return; labels+=("$2") ;;
          --fix-version) validate_fix_version "$2" || return; fix_versions+=("$2") ;;
          --assignee) validate_assignee "$2" || return; assignee_mode="$2" ;;
          --assignee-account-id) validate_non_empty --assignee-account-id "$2" || return
            assignee_mode="account-id"; assignee_account_id="$2" ;;
          --parent) parent="$(issue_key "$2")" ;;
        esac
        shift 2
        ;;
      --allow-non-english) allow_non_english=true; shift ;;
      --dry-run) dry_run=true; shift ;;
      --allow-mutate) allow_mutate=true; shift ;;
      --help|-h) usage; return 0 ;;
      *) echo "jira-api: unknown create-issue argument: $1" >&2; return 2 ;;
    esac
  done

  validate_non_empty project "$project" || return
  validate_non_empty type "$issue_type" || return
  validate_non_empty summary "$summary" || return
  description_json="$(build_content_json "$description" "$description_file" "$description_adf_file")" || return
  validate_english_text summary "$summary" "$allow_non_english" || return
  validate_english_json description "$description_json" "$allow_non_english" || return
  language="$(content_language "$allow_non_english")"
  labels_json="$(json_string_array "${labels[@]}")"
  fix_versions_json="$(json_fix_versions "${fix_versions[@]}")"
  assignee_json="$(assignee_json_for_dry_run "$assignee_mode" "$assignee_account_id")"
  payload="$(build_create_issue_payload "$project" "$issue_type" "$summary" "$parent" "$description_json" \
    "$labels_json" "$assignee_json" "$fix_versions_json")"

  if [[ "$dry_run" == true ]]; then
    jq -n --arg language "$language" --argjson payload "$payload" \
      '{dryRun: true, contentLanguage: $language, payload: $payload}'
    return
  fi
  require_mutation_approval "$allow_mutate" create-issue || return
  if [[ "$assignee_mode" != "" ]]; then
    assignee_json="$(assignee_json_for_network "$assignee_mode" "$assignee_account_id")"
    payload="$(build_create_issue_payload "$project" "$issue_type" "$summary" "$parent" "$description_json" \
      "$labels_json" "$assignee_json" "$fix_versions_json")"
  fi
  jira_request POST "/rest/api/3/issue" "$payload" |
    jq --arg language "$language" '{id, key, self, contentLanguage: $language}'
}

edit_issue_command() {
  local key="$1"
  shift
  local summary="" description="" description_file="" description_adf_file="" parent=""
  local assignee_mode="" assignee_account_id="" dry_run=false allow_mutate=false allow_non_english=false
  local clear_labels=false replace_labels=false clear_fix_versions=false replace_fix_versions=false clear_parent=false
  local description_json labels_json assignee_json fix_versions_json payload language
  local -a labels=()
  local -a fix_versions=()

  while (($#)); do
    case "$1" in
      --summary|--description|--description-file|--description-adf-file|--label|--fix-version|--assignee|\
      --assignee-account-id|--parent)
        [[ $# -ge 2 ]] || { echo "jira-api: $1 requires a value" >&2; return 2; }
        case "$1" in
          --summary) summary="$2" ;;
          --description) description="$2" ;;
          --description-file) description_file="$2" ;;
          --description-adf-file) description_adf_file="$2" ;;
          --label) validate_label "$2" || return; labels+=("$2"); replace_labels=true ;;
          --fix-version) validate_fix_version "$2" || return; fix_versions+=("$2"); replace_fix_versions=true ;;
          --assignee) validate_assignee "$2" || return; assignee_mode="$2" ;;
          --assignee-account-id) validate_non_empty --assignee-account-id "$2" || return
            assignee_mode="account-id"; assignee_account_id="$2" ;;
          --parent) parent="$(issue_key "$2")" ;;
        esac
        shift 2
        ;;
      --clear-labels) clear_labels=true; replace_labels=true; shift ;;
      --clear-fix-versions) clear_fix_versions=true; replace_fix_versions=true; shift ;;
      --clear-parent) clear_parent=true; shift ;;
      --allow-non-english) allow_non_english=true; shift ;;
      --dry-run) dry_run=true; shift ;;
      --allow-mutate) allow_mutate=true; shift ;;
      --help|-h) usage; return 0 ;;
      *) echo "jira-api: unknown edit-issue argument: $1" >&2; return 2 ;;
    esac
  done

  if [[ "$clear_labels" == true && ${#labels[@]} -gt 0 ]]; then
    echo "jira-api: --clear-labels cannot be combined with --label" >&2
    return 2
  fi
  if [[ "$clear_fix_versions" == true && ${#fix_versions[@]} -gt 0 ]]; then
    echo "jira-api: --clear-fix-versions cannot be combined with --fix-version" >&2
    return 2
  fi
  if [[ "$clear_parent" == true && -n "$parent" ]]; then
    echo "jira-api: --clear-parent cannot be combined with --parent" >&2
    return 2
  fi

  description_json="$(build_content_json "$description" "$description_file" "$description_adf_file")" || return
  validate_english_text summary "$summary" "$allow_non_english" || return
  validate_english_json description "$description_json" "$allow_non_english" || return
  language="$(content_language "$allow_non_english")"
  labels_json="$(json_string_array "${labels[@]}")"
  fix_versions_json="$(json_fix_versions "${fix_versions[@]}")"
  assignee_json="$(assignee_json_for_dry_run "$assignee_mode" "$assignee_account_id")"
  payload="$(build_edit_issue_payload "$summary" "$parent" "$clear_parent" "$description_json" "$labels_json" \
    "$replace_labels" "$assignee_json" "$fix_versions_json" "$replace_fix_versions")"
  [[ "$(jq '.fields | length' <<<"$payload")" -gt 0 ]] || {
    echo "jira-api: edit-issue needs at least one editable field" >&2
    return 2
  }
  if [[ "$dry_run" == true ]]; then
    jq -n --arg key "$key" --arg language "$language" --argjson payload "$payload" \
      '{dryRun: true, issueKey: $key, contentLanguage: $language, payload: $payload}'
    return
  fi
  require_mutation_approval "$allow_mutate" edit-issue || return
  if [[ "$assignee_mode" != "" ]]; then
    assignee_json="$(assignee_json_for_network "$assignee_mode" "$assignee_account_id")"
    payload="$(build_edit_issue_payload "$summary" "$parent" "$clear_parent" "$description_json" "$labels_json" \
      "$replace_labels" "$assignee_json" "$fix_versions_json" "$replace_fix_versions")"
  fi
  jira_request PUT "/rest/api/3/issue/${key}" "$payload" >/dev/null
  jq -n --arg key "$key" --arg language "$language" \
    '{key: $key, updated: true, contentLanguage: $language}'
}

comment_issue_command() {
  local key="$1"
  shift
  local body="" body_file="" body_adf_file="" dry_run=false allow_mutate=false allow_non_english=false
  local body_json payload language

  while (($#)); do
    case "$1" in
      --body|--body-file|--body-adf-file)
        [[ $# -ge 2 ]] || { echo "jira-api: $1 requires a value" >&2; return 2; }
        case "$1" in
          --body) body="$2" ;;
          --body-file) body_file="$2" ;;
          --body-adf-file) body_adf_file="$2" ;;
        esac
        shift 2
        ;;
      --allow-non-english) allow_non_english=true; shift ;;
      --dry-run) dry_run=true; shift ;;
      --allow-mutate) allow_mutate=true; shift ;;
      --help|-h) usage; return 0 ;;
      *) echo "jira-api: unknown comment-issue argument: $1" >&2; return 2 ;;
    esac
  done

  body_json="$(build_content_json "$body" "$body_file" "$body_adf_file")" || return
  [[ "$body_json" != null ]] || {
    echo "jira-api: comment-issue requires --body, --body-file, or --body-adf-file" >&2
    return 2
  }
  validate_english_json comment "$body_json" "$allow_non_english" || return
  language="$(content_language "$allow_non_english")"
  payload="$(build_comment_payload "$body_json")"
  if [[ "$dry_run" == true ]]; then
    jq -n --arg key "$key" --arg language "$language" --argjson payload "$payload" \
      '{dryRun: true, issueKey: $key, contentLanguage: $language, payload: $payload}'
    return
  fi
  require_mutation_approval "$allow_mutate" comment-issue || return
  jira_request POST "/rest/api/3/issue/${key}/comment" "$payload" |
    jq --arg key "$key" --arg language "$language" \
      '{issueKey: $key, id, self, created, author: (.author.displayName // null), contentLanguage: $language}'
}

transitions_command() {
  local key="$1"
  jira_request GET "/rest/api/3/issue/${key}/transitions?expand=transitions.fields" |
    jq --arg key "$key" '{
      issueKey: $key,
      transitions: [
        .transitions[]? | {
          id,
          name,
          to: .to.name,
          statusId: .to.id,
          hasScreen: (.hasScreen // false),
          fields: (.fields | keys)
        }
      ]
    }'
}

transition_issue_command() {
  local key="$1"
  shift
  local target="" dry_run=false allow_mutate=false transitions_json transition_id transition_name transition_to payload

  while (($#)); do
    case "$1" in
      --to)
        [[ $# -ge 2 ]] || { echo "jira-api: --to requires a value" >&2; return 2; }
        target="$2"
        shift 2
        ;;
      --dry-run) dry_run=true; shift ;;
      --allow-mutate) allow_mutate=true; shift ;;
      --help|-h) usage; return 0 ;;
      *) echo "jira-api: unknown transition-issue argument: $1" >&2; return 2 ;;
    esac
  done
  validate_non_empty --to "$target" || return
  if [[ "$dry_run" != true ]]; then
    require_mutation_approval "$allow_mutate" transition-issue || return
  fi

  transitions_json="$(jira_request GET "/rest/api/3/issue/${key}/transitions?expand=transitions.fields")"
  transition_id="$(jq -r --arg target "$target" '
    .transitions[]?
    | select(((.id | tostring) == $target) or (.name == $target) or (.to.name == $target))
    | (.id | tostring)
  ' <<<"$transitions_json" | head -n 1)"
  if [[ -z "$transition_id" ]]; then
    echo "jira-api: transition not found: $target" >&2
    jq '{availableTransitions: [.transitions[]? | {id, name, to: .to.name}]}' <<<"$transitions_json" >&2
    return 2
  fi
  transition_name="$(jq -r --arg id "$transition_id" '.transitions[] | select((.id | tostring) == $id) | .name' <<<"$transitions_json")"
  transition_to="$(jq -r --arg id "$transition_id" '.transitions[] | select((.id | tostring) == $id) | .to.name' <<<"$transitions_json")"
  payload="$(jq -n --arg id "$transition_id" '{transition: {id: $id}}')"
  if [[ "$dry_run" == true ]]; then
    jq -n --arg key "$key" --arg name "$transition_name" --arg to "$transition_to" --argjson payload "$payload" '{
      dryRun: true,
      issueKey: $key,
      transition: {id: $payload.transition.id, name: $name, to: $to},
      payload: $payload
    }'
  else
    jira_request POST "/rest/api/3/issue/${key}/transitions" "$payload" >/dev/null
    jq -n --arg key "$key" --arg id "$transition_id" --arg name "$transition_name" --arg to "$transition_to" '{
      key: $key,
      transitioned: true,
      transition: {id: $id, name: $name, to: $to}
    }'
  fi
}

require_command jq
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
        read_only: false,
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
  transitions)
    key="$(issue_key "${2:-}")"
    transitions_command "$key"
    ;;
  transition-issue)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    key="$(issue_key "$2")"
    shift 2
    transition_issue_command "$key" "$@"
    ;;
  create-issue)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    create_issue_command "${@:2}"
    ;;
  edit-issue)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    key="$(issue_key "$2")"
    shift 2
    edit_issue_command "$key" "$@"
    ;;
  comment-issue)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    key="$(issue_key "$2")"
    shift 2
    comment_issue_command "$key" "$@"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
