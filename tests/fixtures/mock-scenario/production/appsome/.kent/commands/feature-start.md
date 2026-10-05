# Role and Objective

You are running `/prompt:feature-start`. Prepare a feature workspace from `$ARGUMENTS` and produce implementation-ready local artifacts: design, spec, and plan.

The desired outcome is a `.todo/<feature>/` workspace with enough context for `/prompt:feature-implement` to start coding without re-fetching everything.

# Inputs

- `$ARGUMENTS`: Figma URLs, Jira URLs/issue keys, spec paths/URLs, or a feature description.
- Optional explicit feature name or `.todo/<feature>` workspace path in `$ARGUMENTS`.
- Internal recipes:
  - `.kent/skills/appsome-android-workflow/references/recipes/feature-init.md`
  - `.kent/skills/appsome-android-workflow/references/recipes/feature-design.md`
  - `.kent/skills/appsome-android-workflow/references/recipes/feature-spec.md`
  - `.kent/skills/appsome-android-workflow/references/recipes/feature-plan.md`

# Kent Runtime Rules

- Follow `.kent/skills/appsome-android-workflow/SKILL.md`.
- Load `.kent/skills/appsome-android-workflow/references/rules/feature-target-resolution.md` before creating or reusing a feature workspace.
- Load the four internal recipes lazily and in phase order. Do not expose those recipes as public `/prompt:*` commands.
- Load `.kent/skills/appsome-android-workflow/references/rules/serena.md` before codebase analysis when planning touches existing Kotlin files or contracts.
- Load `.kent/skills/appsome-android-workflow/references/rules/shared-brain.md` when the feature may need optional
  PRD/TRD/RFC/story knowledge from the local Shared Brain repository.
- Load `.kent/skills/appsome-android-workflow/references/rules/jira.md` when `$ARGUMENTS` contains a Jira URL, JQL,
  ticket wording, or an issue key such as `MBL-718`.
- Load `.kent/skills/appsome-android-workflow/references/rules/web-access-policy.md` before fetching any non-Figma
  URL extracted from Jira.
- Use `android-researcher` and `ios-reference` subagents when planning requires codebase or iOS analysis.
- Use Figma MCP only through `~/.kent/bin/kent-mcp-call`. Persist only known-safe, task-scoped artifacts under
  `.todo/<feature>/mcp`, and redirect raw command stdout when the saved artifact is the intended consumer.
- If MCP is unavailable, save partial progress and report the blocker instead of inventing design details.
- Do not implement Android code in this command.
- Do not commit or push unless explicitly requested.

# Recipe Loading

Recipe directory: `.kent/skills/appsome-android-workflow/references/recipes/`.

Load these internal recipes in order, one phase at a time:

1. `feature-init.md`
2. `feature-design.md`
3. `feature-spec.md`
4. `feature-plan.md`

The command must follow the loaded recipe for each phase instead of recreating the detailed phase logic from memory.

# Workflow

## 1. Bootstrap

1. Resolve the feature target from `$ARGUMENTS` using `feature-target-resolution.md`. If `$ARGUMENTS` has no feature name, ask for one and normalize it to kebab-case.
2. Ensure `.todo/` exists and is ignored by git.
3. Create `.todo/<feature>/` and `meta.json` if missing.
4. Preserve existing feature artifacts unless the user explicitly asks for refresh or replacement.

## 2. Detect Task Shape

Before task-shape detection, ingest Jira source context when `$ARGUMENTS` contains Jira URLs or issue keys:

1. Use `references/rules/jira.md`.
2. Define the root issue set from only the issue keys/URLs explicitly supplied
   in `$ARGUMENTS`, the task source/body, or an exact human-authored task
   comment. Only root issues contribute implementation requirements.
3. Split Jira inputs by source type:
   - Issue keys, `/browse/<KEY>` URLs, and board URLs with `selectedIssue=<KEY>`: fetch each issue with
     `.kent/adapters/jira/jira-api.sh issue <KEY>`.
   - Board URLs without `selectedIssue`: fetch board metadata with `.kent/adapters/jira/jira-api.sh board <URL>` and,
     when the prompt includes status/assignee/limit hints, fetch task candidates with
     `.kent/adapters/jira/jira-api.sh board-issues <URL> --status '<STATUS>' --assignee me --limit <N>`.
4. For board prompts, map natural language hints conservatively:
   - `Ready for dev` or similar status text -> `--status 'Ready for dev'`
   - `assigned to me`, `на меня` -> `--assignee me`
   - `first 3`, `первые 3` -> `--limit 3`
5. For every root issue, inspect normalized `issue_links`, follow only one
   graph level, and fetch at most `10` related issues. Classify sibling
   platforms from linked issue metadata, not relationship wording alone.
   Related issues remain evidence, dependencies, or deferred scope unless the
   root set explicitly includes them.
6. Write `.todo/<feature>/jira.md` with separate `Root Scope`, `Related
   Evidence`, `Dependencies`, and `Deferred / Out Of Scope` sections plus issue
   contexts and/or board listing contexts:
   - issue context: issue key, summary, status, type, labels, components, fix
     versions, description text, extracted URLs, normalized relationships,
     fetched sibling issue summaries, URL classifications, and
     unresolved/private links;
   - board context: board id, project key, filters, limit, ordered candidate issues with key, summary, status, type,
     assignee, and parent, and a note that this is read-only candidate context.
7. Add extracted Figma URLs to the design URL set and annotate them in `.todo/<feature>/url-annotations.md` with
   `source: Jira <KEY>`.
8. Treat extracted Notion/spec URLs as spec candidates only after applying `web-access-policy.md` and available MCP/auth
   constraints.
9. Record GitHub, Jira, internal, and unknown links as found but not fetched unless the user explicitly approves fetching
   them or a project-approved adapter handles them.

- Visual feature: Figma URL is present, or user mentions screen, design, UI, layout, or flow.
- Code-only feature: no Figma URL and user mentions refactor, migrate, replace, extract, split, universal component, or consolidate.
- If unclear, ask whether there is a design or this is code-only.

In generated Engineering Delivery, `work_kind` routing is already authoritative.
Do not recreate, move, or redirect the task from this procedure. In standalone
manual use, an explicit refactor or migration may be reported as better suited
to its dedicated procedure, but do not invoke another `/prompt:*` flow.

For a non-visual feature, skip design but still create `spec.md` from the task description or interview before planning.

## 3. Design Phase

1. Extract all Figma URLs from `$ARGUMENTS` and `.todo/<feature>/jira.md` when present.
2. Deduplicate URLs by `node-id`.
3. Preserve user annotations and grouping in `.todo/<feature>/url-annotations.md`.
4. Follow `feature-design.md` exactly for Figma MCP calls and artifact generation.
5. Verify expected artifacts:
   - `.todo/<feature>/design.md`
   - `.todo/<feature>/layouts.md`
   - `.todo/<feature>/nodes.json`
   - `.todo/<feature>/screenshots/` when screenshots are available

If no design exists, record that explicitly and continue only when the spec/plan can be produced without it.

## 4. Spec Phase

1. If `$ARGUMENTS` or `.todo/<feature>/jira.md` includes a spec path or URL, follow `feature-spec.md` with that source.
2. If no spec source exists, use the recipe's interview mode after design is loaded.
3. Ask only questions not answerable from the design or provided context.
4. Verify `.todo/<feature>/spec.md` exists unless the feature is explicitly code-only.

## 5. Plan Phase

1. Follow `feature-plan.md`.
2. Use Serena for targeted Kotlin symbol overviews, references, declarations, and implementations before planning changes to existing contracts.
3. Use codebase and iOS subagents only for targeted analysis.
4. When Jira identifies an iOS/web sibling task, load
   `references/rules/local-reference-projects.md`, locate the implementation
   through the sibling issue before heuristic search, and inspect focused
   production sources and tests. Write
   `.todo/<feature>/reference-implementations.md` with exact commits/paths and
   `Checked`, `Adopted`, `Rejected`, and `Conflicts`.
5. If no useful sibling relation exists but a related implementation is likely,
   use a bounded API/model/flow fingerprint search and record attempted queries.
6. If Shared Brain was relevant, ensure `.todo/<feature>/shared-brain.md` exists with findings or an
   unavailable/skipped status.
7. If Jira context was used, ensure `.todo/<feature>/jira.md` exists and is considered as source context without
   overriding explicit user instructions, Figma design, or API source-of-truth data.
8. Add an explicit scope boundary to `spec.md` and `plan.md`: root issue IDs,
   related evidence, dependencies, and deferred issues. Shared parent/design/API
   context does not merge those issue scopes.
9. Inventory SDK/schema/version changes and adaptations outside root-owned
   behavior. Keep compatibility edits bounded and separate from deferred
   product requirements.
10. Preserve generated enum/sealed provider, status, action, flow, placement,
    and intent contracts through domain planning; do not plan normalized string
    routing when a typed source exists.
11. Read `.kent/commands/mock-scenario-policy.md`. Inventory each planned
    behavior delta with a stable behavior ID and initial applicability; specify
    the mock-backed scenario/assertions or concrete per-behavior
    non-applicability rationale and alternative verification.
12. Generate `.todo/<feature>/plan.md` with concrete steps, file paths, dependencies, verification commands, and
    the behavior inventory required by the policy.
13. Verify the plan exists and is implementation-ready.

# Output Format

Final response:

- Feature name.
- Design/spec/plan artifact status.
- Plan step summary.
- Blockers or open questions.
- Next command to run, usually `/prompt:feature-implement`.
