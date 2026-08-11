#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  sentry-issues.sh --self-test
  sentry-issues.sh --check-auth
  sentry-issues.sh candidates [--query <QUERY>] [--include-seen] [--limit <1..100>]
  sentry-issues.sh list [--query <QUERY>] [--limit <1..100>]
  sentry-issues.sh issue <ISSUE_ID_OR_CANONICAL_URL>
  sentry-issues.sh latest-event <ISSUE_ID_OR_CANONICAL_URL> [--frames <0..50>]
  sentry-issues.sh mark-seen <ISSUE_ID_OR_CANONICAL_URL> [--dry-run|--allow-mutate]
  sentry-issues.sh mark-unseen <ISSUE_ID_OR_CANONICAL_URL> [--dry-run|--allow-mutate]
  sentry-issues.sh resolve <ISSUE_ID_OR_CANONICAL_URL> [--next-release] [--dry-run|--allow-mutate]
  sentry-issues.sh mute <ISSUE_ID_OR_CANONICAL_URL> [--dry-run|--allow-mutate]
  sentry-issues.sh unresolve <ISSUE_ID_OR_CANONICAL_URL> [--dry-run|--allow-mutate]

The project profile may contain only:
  [integrations.sentry]
  base_url = "https://sentry.example.invalid"
  organization = "example"
  project = "mobile"
  credential_namespace = "EXAMPLE"
USAGE
}

die() {
  printf 'sentry-issues: %s\n' "$1" >&2
  exit "${2:-1}"
}

require_command() {
  local command_name="$1"
  command -v "$command_name" >/dev/null 2>&1 ||
    die "missing command: $command_name" 127
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
    die "KENT_ENGINEERING_KIT_PYTHON must be Python 3.11 or newer"
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

  die "Python 3.11 or newer is required to read the project profile"
}

project_profile_path() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  printf '%s/.kent/workflow-profile.toml\n' "$root"
}

profile_sentry_json() {
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
sentry = profile.get("integrations", {}).get("sentry", {})
if not isinstance(sentry, dict):
    raise SystemExit("integrations.sentry must be a TOML table")

allowed = {"base_url", "organization", "project", "credential_namespace"}
unknown = sorted(set(sentry) - allowed)
if unknown:
    raise SystemExit(
        "unsupported integrations.sentry keys: " + ", ".join(unknown)
    )
values = {
    key: value
    for key, value in sentry.items()
    if key in allowed and isinstance(value, str) and value.strip()
}
print(json.dumps(values))
PY
}

profile_value() {
  local key="$1"
  jq -r --arg key "$key" '.[$key] // empty' <<<"$PROFILE_SENTRY_JSON"
}

load_configuration() {
  PROFILE_SENTRY_JSON="$(profile_sentry_json)"
  SENTRY_BASE_URL="$(profile_value base_url)"
  SENTRY_ORGANIZATION="$(profile_value organization)"
  SENTRY_PROJECT="$(profile_value project)"
  CREDENTIAL_NAMESPACE="$(profile_value credential_namespace)"

  if [[ -z "$SENTRY_BASE_URL" ]]; then
    SENTRY_BASE_URL="https://sentry.io"
  fi
  SENTRY_BASE_URL="${SENTRY_BASE_URL%/}"
  if [[ -n "$CREDENTIAL_NAMESPACE" &&
    ! "$CREDENTIAL_NAMESPACE" =~ ^[A-Za-z][A-Za-z0-9_.-]*$ ]]; then
    die "integrations.sentry.credential_namespace contains unsafe characters"
  fi
}

require_network_configuration() {
  require_command curl
  [[ "$SENTRY_BASE_URL" =~ ^https?://[^/]+$ ]] ||
    die "integrations.sentry.base_url must be an absolute HTTP(S) URL"
  [[ -n "$SENTRY_ORGANIZATION" ]] ||
    die "integrations.sentry.organization is not configured"
  [[ -n "$SENTRY_PROJECT" ]] ||
    die "integrations.sentry.project is not configured"
}

indirect_value() {
  local variable_name="$1"
  [[ "$variable_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 0
  printf '%s' "${!variable_name-}"
}

namespace_prefixes() {
  local normalized
  [[ -n "$CREDENTIAL_NAMESPACE" ]] || return 0
  if [[ "$CREDENTIAL_NAMESPACE" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    printf '%s\n' "$CREDENTIAL_NAMESPACE"
  fi
  normalized="$(printf '%s' "$CREDENTIAL_NAMESPACE" | tr '[:lower:]-.' '[:upper:]__')"
  if [[ "$normalized" =~ ^[A-Za-z_][A-Za-z0-9_]*$ &&
    "$normalized" != "$CREDENTIAL_NAMESPACE" ]]; then
    printf '%s\n' "$normalized"
  fi
}

auth_token_from_environment() {
  local variable_name value prefix
  value="$(indirect_value KENT_SENTRY_AUTH_TOKEN)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return
  fi
  while IFS= read -r prefix; do
    [[ -n "$prefix" ]] || continue
    variable_name="${prefix}_SENTRY_AUTH_TOKEN"
    value="$(indirect_value "$variable_name")"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
  done < <(namespace_prefixes)
  value="$(indirect_value SENTRY_AUTH_TOKEN)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  fi
}

auth_ref_from_environment() {
  local variable_name value prefix
  value="$(indirect_value KENT_SENTRY_AUTH_TOKEN_OP_REF)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return
  fi
  while IFS= read -r prefix; do
    [[ -n "$prefix" ]] || continue
    variable_name="${prefix}_SENTRY_AUTH_TOKEN_OP_REF"
    value="$(indirect_value "$variable_name")"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
  done < <(namespace_prefixes)
  value="$(indirect_value SENTRY_AUTH_TOKEN_OP_REF)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  fi
}

read_auth_token() {
  local token ref ref_file mode
  token="$(auth_token_from_environment)"
  if [[ -n "$token" ]]; then
    printf '%s' "$token"
    return
  fi

  ref="$(auth_ref_from_environment)"
  if [[ -z "$ref" && -n "$CREDENTIAL_NAMESPACE" ]]; then
    ref_file="$HOME/.kent/credentials/sentry/$(
      printf '%s' "$CREDENTIAL_NAMESPACE" | tr '[:upper:]' '[:lower:]'
    ).opref"
    if [[ -f "$ref_file" ]]; then
      [[ ! -L "$ref_file" ]] ||
        die "local Sentry credential reference must not be a symlink"
      if mode="$(stat -f '%Lp' "$ref_file" 2>/dev/null)"; then
        :
      else
        mode="$(stat -c '%a' "$ref_file" 2>/dev/null)" ||
          die "unable to inspect local Sentry credential reference permissions"
      fi
      [[ "$mode" == "600" ]] ||
        die "local Sentry credential reference must use mode 0600"
      ref="$(tr -d '\r\n' <"$ref_file")"
      [[ "$ref" == op://* ]] ||
        die "local Sentry credential reference must contain one op:// reference"
    fi
  fi
  [[ -n "$ref" ]] || die "missing Sentry auth token"
  require_command op
  if ! token="$(op read "$ref")"; then
    die "unable to resolve Sentry auth token from 1Password"
  fi
  token="$(printf '%s' "$token" | tr -d '\r\n')"
  [[ -n "$token" ]] || die "1Password returned an empty Sentry auth token"
  printf '%s' "$token"
}

load_auth_token() {
  AUTH_TOKEN="$(read_auth_token)"
}

urlencode() {
  jq -rn --arg value "$1" '$value | @uri'
}

issue_id() {
  local value="${1:-}" parsed authority base_authority organization_authority
  if [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\n' "$value"
    return
  fi

  [[ "$value" =~ ^https?:// ]] ||
    die "expected one numeric Sentry issue ID or canonical Sentry issue URL" 64
  authority="$(
    printf '%s\n' "$value" |
      sed -nE 's#^https?://([^/]+)/.*#\1#p' |
      tr '[:upper:]' '[:lower:]'
  )"
  base_authority="$(
    printf '%s\n' "$SENTRY_BASE_URL" |
      sed -nE 's#^https?://([^/]+)$#\1#p' |
      tr '[:upper:]' '[:lower:]'
  )"
  organization_authority="$(
    printf '%s.%s' "$SENTRY_ORGANIZATION" "$base_authority" |
      tr '[:upper:]' '[:lower:]'
  )"
  [[ "$authority" == "$base_authority" ||
    "$authority" == "$organization_authority" ]] ||
    die "Sentry issue URL must use the configured Sentry tenant" 64
  parsed="$(
    printf '%s\n' "$value" |
      sed -nE 's#^https?://[^/]+/(.*\/)?issues/([1-9][0-9]+)/?([?].*)?$#\2#p'
  )"
  [[ -n "$parsed" ]] ||
    die "expected one numeric Sentry issue ID or canonical Sentry issue URL" 64
  printf '%s\n' "$parsed"
}

curl_with_auth() {
  local escaped_token="$AUTH_TOKEN"
  escaped_token="${escaped_token//\\/\\\\}"
  escaped_token="${escaped_token//\"/\\\"}"
  printf 'header = "Authorization: Bearer %s"\n' "$escaped_token" |
    curl --config - "$@"
}

api_get() {
  local path="$1"
  curl_with_auth \
    --silent \
    --show-error \
    --fail \
    --connect-timeout 15 \
    --max-time 90 \
    --retry 2 \
    --retry-all-errors \
    --request GET \
    --header "Accept: application/json" \
    "${SENTRY_BASE_URL}${path}"
}

api_put_has_seen() {
  local issue="$1" desired="$2" organization
  organization="$(urlencode "$SENTRY_ORGANIZATION")"
  curl_with_auth \
    --silent \
    --show-error \
    --fail \
    --connect-timeout 15 \
    --max-time 90 \
    --retry 2 \
    --retry-all-errors \
    --request PUT \
    --header "Accept: application/json" \
    --header "Content-Type: application/json" \
    --data "{\"hasSeen\":$desired}" \
    "${SENTRY_BASE_URL}/api/0/organizations/${organization}/issues/${issue}/" >/dev/null
}

# shellcheck disable=SC2016
normalized_issue_jq='
def bounded_text($size):
  if . == null then null
  else
    tostring
    | if length > $size then .[0:$size] + "…" else . end
  end;
def project_summary:
  if . == null then null
  elif type == "object" then {
    id: (.id // null),
    slug: (.slug // null),
    name: (.name // null)
  }
  else {slug: tostring}
  end;
{
  id: (if .id == null then null else (.id | tostring) end),
  short_id: (.shortId // null),
  title: ((.title // null) | bounded_text(500)),
  culprit: ((.culprit // null) | bounded_text(500)),
  permalink: (.permalink // null),
  status: (.status // null),
  substatus: (.substatus // null),
  status_details: (
    if (.statusDetails? | type) == "object" then {
      in_release: (.statusDetails.inRelease // null),
      in_next_release: (.statusDetails.inNextRelease // null),
      ignore_count: (.statusDetails.ignoreCount // null),
      ignore_window: (.statusDetails.ignoreWindow // null),
      ignore_user_count: (.statusDetails.ignoreUserCount // null),
      ignore_user_window: (.statusDetails.ignoreUserWindow // null)
    } else null end
  ),
  level: (.level // null),
  platform: (.platform // null),
  project: (.project | project_summary),
  count: (.count // null),
  user_count: (.userCount // null),
  first_seen: (.firstSeen // null),
  last_seen: (.lastSeen // null),
  has_seen: .hasSeen,
  is_bookmarked: .isBookmarked,
  is_subscribed: .isSubscribed
}'

issues_from_response_jq='
if type == "array" then .
elif (.issues? | type) == "array" then .issues
else []
end
| map('"$normalized_issue_jq"')
'

api_issue() {
  local issue="$1" organization
  organization="$(urlencode "$SENTRY_ORGANIZATION")"
  api_get "/api/0/organizations/${organization}/issues/${issue}/" |
    jq -c "$normalized_issue_jq"
}

api_candidates() {
  local include_seen="$1" limit="$2" requested_query="$3"
  local organization project query response
  organization="$(urlencode "$SENTRY_ORGANIZATION")"
  project="$(urlencode "$SENTRY_PROJECT")"
  query="$(urlencode "$requested_query")"
  response="$(
    api_get "/api/0/organizations/${organization}/issues/?project=${project}&query=${query}&limit=${limit}"
  )"
  jq -c --argjson include_seen "$include_seen" \
    "$issues_from_response_jq
     | if \$include_seen then . else map(select(.has_seen != true)) end
    " <<<"$response"
}

# shellcheck disable=SC2016
latest_event_jq='
def bounded_text($size):
  if . == null then null
  else
    tostring
    | if length > $size then .[0:$size] + "…" else . end
  end;
def exception_values:
  if ((.exception? | type) == "object"
      and (.exception.values? | type) == "array") then
    .exception.values
  else
    [(.entries // [])[] | select(.type? == "exception") |
      (.data.values? // [])] | add // []
  end;
def safe_mechanism:
  if type == "object" then {
    type: ((.type // null) | bounded_text(200)),
    handled: .handled,
    synthetic: .synthetic,
    description: ((.description // null) | bounded_text(500))
  } else null end;
def safe_frame:
  {
    filename: ((.filename // null) | bounded_text(500)),
    function: ((.function // null) | bounded_text(300)),
    module: ((.module // null) | bounded_text(300)),
    package: ((.package // null) | bounded_text(300)),
    abs_path: ((.absPath // .abs_path // null) | bounded_text(500)),
    line: (.lineNo // .lineno // null),
    column: (.colNo // .colno // null),
    in_app: (.inApp // .in_app // false)
  };
def all_frames:
  [exception_values[] |
    ((.stacktrace.frames // [])[]? |
      select((.inApp // .in_app // false) == true) | safe_frame)];
{
  event_id: (.eventID // .event_id // .id // null),
  issue_id: (.groupID // .group_id // null),
  platform: (.platform // null),
  timestamp: (.dateCreated // .timestamp // null),
  release: (
    if (.release? | type) == "object" then
      ((.release.version // .release.shortVersion // null) | bounded_text(300))
    else
      ((.release // null) | bounded_text(300))
    end
  ),
  environment: (
    (
      .environment
      // ([.tags[]? | select(.key == "environment") | .value][0] // null)
    ) | bounded_text(200)
  ),
  exceptions: [
    exception_values[] | {
      type: ((.type // null) | bounded_text(200)),
      value: ((.value // null) | bounded_text(1000)),
      mechanism: (.mechanism | safe_mechanism)
    }
  ],
  frames: (all_frames | if $frames == 0 then [] else .[-$frames:] end)
}'

api_latest_event() {
  local issue="$1" frames="$2" organization
  organization="$(urlencode "$SENTRY_ORGANIZATION")"
  api_get "/api/0/organizations/${organization}/issues/${issue}/events/latest/" |
    jq -c --argjson frames "$frames" "$latest_event_jq"
}

check_auth() {
  local organization response
  require_network_configuration
  load_auth_token
  organization="$(urlencode "$SENTRY_ORGANIZATION")"
  response="$(api_get "/api/0/organizations/${organization}/")"
  jq -c '
    {
      status: "ok",
      authenticated: true,
      organization: {
        id: (.id // null),
        slug: (.slug // null),
        name: (.name // null)
      }
    }
  ' <<<"$response"
}

run_sentry_cli_mutation() {
  local action="$1" issue="$2" next_release="$3"
  local -a arguments
  case "$action" in
    resolve|mute|unresolve) ;;
    *) die "unsupported Sentry CLI mutation: $action" ;;
  esac

  arguments=(
    issues "$action"
    --id "$issue"
    --org "$SENTRY_ORGANIZATION"
    --project "$SENTRY_PROJECT"
  )
  if [[ "$action" == "resolve" && "$next_release" == true ]]; then
    arguments+=(--next-release)
  fi

  env \
    "SENTRY_URL=$SENTRY_BASE_URL" \
    "SENTRY_ORG=$SENTRY_ORGANIZATION" \
    "SENTRY_PROJECT=$SENTRY_PROJECT" \
    "SENTRY_AUTH_TOKEN=$AUTH_TOKEN" \
    sentry-cli "${arguments[@]}" >/dev/null
}

verify_status() {
  local action="$1" normalized="$2" status
  status="$(jq -r '.status // empty' <<<"$normalized")"
  case "$action:$status" in
    resolve:resolved|unresolve:unresolved|mute:ignored|mute:muted) return 0 ;;
  esac
  die "Sentry mutation verification failed for $action (status=$status)"
}

mutate_has_seen() {
  local action="$1" input="$2" allow_mutate="$3" dry_run="$4"
  local issue desired verified
  issue="$(issue_id "$input")"
  if [[ "$dry_run" == true ]]; then
    jq -cn --arg action "$action" --arg id "$issue" \
      '{action: $action, issue_id: $id, dry_run: true, would_mutate: true}'
    return
  fi
  [[ "$allow_mutate" == true ]] ||
    die "$action requires --allow-mutate" 64

  require_network_configuration
  load_auth_token
  desired=false
  [[ "$action" == "mark-seen" ]] && desired=true
  api_put_has_seen "$issue" "$desired"
  verified="$(api_issue "$issue")"
  [[ "$(jq -r 'if .has_seen == null then "" else (.has_seen | tostring) end' <<<"$verified")" == "$desired" ]] ||
    die "Sentry mutation verification failed for $action"
  jq --arg action "$action" \
    '. + {action: $action, dry_run: false, verified: true}' <<<"$verified"
}

mutate_issue_state() {
  local action="$1" input="$2" allow_mutate="$3" dry_run="$4" next_release="$5"
  local issue verified
  issue="$(issue_id "$input")"
  if [[ "$dry_run" == true ]]; then
    jq -cn --arg action "$action" --arg id "$issue" \
      --argjson nextRelease "$next_release" '{
        action: $action,
        issue_id: $id,
        next_release: $nextRelease,
        dry_run: true,
        would_mutate: true
      }'
    return
  fi
  [[ "$allow_mutate" == true ]] ||
    die "$action requires --allow-mutate" 64

  require_network_configuration
  require_command sentry-cli
  load_auth_token
  run_sentry_cli_mutation "$action" "$issue" "$next_release"
  verified="$(api_issue "$issue")"
  verify_status "$action" "$verified"
  jq --arg action "$action" \
    '. + {action: $action, dry_run: false, verified: true}' <<<"$verified"
}

parse_list_options() {
  local mode="$1"
  shift
  if [[ "$mode" == list ]]; then
    INCLUDE_SEEN=true
  else
    INCLUDE_SEEN=false
  fi
  LIMIT=100
  QUERY="is:unresolved"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --include-seen)
        [[ "$mode" == candidates ]] ||
          die "--include-seen is only valid with candidates" 64
        INCLUDE_SEEN=true
        ;;
      --query)
        [[ $# -ge 2 ]] || die "--query requires a value" 64
        QUERY="$2"
        shift
        ;;
      --limit)
        [[ $# -ge 2 ]] || die "--limit requires a value" 64
        LIMIT="$2"
        shift
        ;;
      *) die "unknown candidates option: $1" 64 ;;
    esac
    shift
  done
  [[ "$LIMIT" =~ ^[0-9]+$ && "$LIMIT" -ge 1 && "$LIMIT" -le 100 ]] ||
    die "--limit must be between 1 and 100" 64
  [[ -n "$QUERY" && "$QUERY" != *$'\n'* && ${#QUERY} -le 1000 ]] ||
    die "--query must be a non-empty single-line value up to 1000 characters" 64
}

parse_frames_options() {
  FRAMES=10
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --frames)
        [[ $# -ge 2 ]] || die "--frames requires a value" 64
        FRAMES="$2"
        shift
        ;;
      *) die "unknown latest-event option: $1" 64 ;;
    esac
    shift
  done
  [[ "$FRAMES" =~ ^[0-9]+$ && "$FRAMES" -le 50 ]] ||
    die "--frames must be between 0 and 50" 64
}

parse_mutation_options() {
  local action="$1"
  shift
  ALLOW_MUTATE=false
  DRY_RUN=false
  NEXT_RELEASE=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --allow-mutate) ALLOW_MUTATE=true ;;
      --dry-run) DRY_RUN=true ;;
      --next-release)
        [[ "$action" == resolve ]] ||
          die "--next-release is only valid with resolve" 64
        NEXT_RELEASE=true
        ;;
      *) die "unknown mutation option: $1" 64 ;;
    esac
    shift
  done
}

self_test() {
  jq -cn \
    --arg base_url "$SENTRY_BASE_URL" \
    --arg organization "$SENTRY_ORGANIZATION" \
    --arg project "$SENTRY_PROJECT" \
    --arg credential_namespace "$CREDENTIAL_NAMESPACE" \
    '{
      status: "ok",
      adapter: "sentry-issues",
      read_only: false,
      supports_dry_run: true,
      base_url: $base_url,
      organization: $organization,
      project: $project,
      credential_namespace: $credential_namespace
    }'
}

require_command jq
load_configuration

[[ $# -gt 0 ]] || {
  usage
  exit 64
}

case "$1" in
  --self-test)
    [[ $# -eq 1 ]] || die "--self-test does not accept arguments" 64
    self_test
    ;;
  --check-auth)
    [[ $# -eq 1 ]] || die "--check-auth does not accept arguments" 64
    check_auth
    ;;
  candidates|list)
    list_mode="$1"
    shift
    parse_list_options "$list_mode" "$@"
    require_network_configuration
    load_auth_token
    api_candidates "$INCLUDE_SEEN" "$LIMIT" "$QUERY"
    ;;
  issue)
    [[ $# -eq 2 ]] || die "issue requires exactly one issue ID or URL" 64
    issue="$(issue_id "$2")"
    require_network_configuration
    load_auth_token
    api_issue "$issue"
    ;;
  latest-event)
    [[ $# -ge 2 ]] || die "latest-event requires one issue ID or URL" 64
    input="$2"
    shift 2
    parse_frames_options "$@"
    issue="$(issue_id "$input")"
    require_network_configuration
    load_auth_token
    api_latest_event "$issue" "$FRAMES"
    ;;
  mark-seen|mark-unseen)
    [[ $# -ge 2 ]] || die "$1 requires one issue ID or URL" 64
    action="$1"
    input="$2"
    shift 2
    parse_mutation_options "$action" "$@"
    mutate_has_seen "$action" "$input" "$ALLOW_MUTATE" "$DRY_RUN"
    ;;
  resolve|mute|unresolve)
    [[ $# -ge 2 ]] || die "$1 requires one issue ID or URL" 64
    action="$1"
    input="$2"
    shift 2
    parse_mutation_options "$action" "$@"
    mutate_issue_state "$action" "$input" "$ALLOW_MUTATE" "$DRY_RUN" "$NEXT_RELEASE"
    ;;
  *)
    usage
    exit 64
    ;;
esac
