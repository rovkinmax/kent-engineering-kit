# Role and Objective

You are running `/prompt:refactor-start`. Plan and execute a refactor described by `$ARGUMENTS` with stepwise verification and no accidental user-work disruption.

The desired outcome is a local refactor workspace, a concrete reviewed plan, and, when approved, atomic code changes that keep the project compiling between steps.

# Inputs

- `$ARGUMENTS`: free-text description of what to refactor and why.
- Optional shared local knowledge:
  - `.todo/_shared/patterns-learned.md`
  - `.todo/_shared/api-quirks.md`
  - `.todo/_shared/ui-decisions.md`

# Kent Runtime Rules

- Follow `.kent/skills/appsome-android-workflow/SKILL.md`.
- Use `kent run --agent android-researcher --workspace "$PWD" "<prompt>"` for bounded read-only codebase analysis.
- Load `.kent/skills/appsome-android-workflow/references/rules/jira.md` when `$ARGUMENTS` contains Jira URLs, ticket
  wording, JQL, or issue keys such as `MBL-718`; treat Jira as read-only source context.
- Use the `/prompt:feature-audit` workflow for mandatory read-only review of every non-trivial refactor plan before
  asking for scope confirmation or starting code edits. Kent may expose it as normalized `/prompt:featureaudit`;
  treat that as the same workflow.
- Use `kent run --agent build-doctor --workspace "$PWD" "<prompt>"` for noisy compile diagnostics.
- Use `~/.kent/bin/kent-mcp-call` and `~/.kent/bin/kent-mcp-list` for MCP access. Do not use raw `mcp__...` tool names.
- Use Serena for semantic symbol discovery, references, implementations, and rare symbol-wide renames. Use Kent `patch` for local edits.
- Do not use `jetbrains.build_project`; use Gradle commands for build verification.
- For warning-removal refactors, add a full warning gate in addition to narrow per-step checks:
  `./tools/agentw :app:detektAll :app:compileDevDebugKotlin :app:testDevDebugUnitTest --warning-mode all` in a
  Kent worktree, or the same tasks through `./gradlew` in the main checkout.
- Do not commit or push unless explicitly requested.

# Recipe Loading

There is no single dedicated refactor recipe. During planning/execution, load only recipes matching the code being changed:

- UI/Compose refactor: `compose-screen.md`, `ui-components.md`, `compose-performance.md`, `compose-previews.md`.
- ViewModel/action/state refactor: `viewmodel.md`, `viewmodel-advanced.md`, `error-handling.md`.
- Domain/entity/mapper refactor: `domain-model.md`, `domain-mapper.md`.
- Data/API refactor: `api-endpoint.md`, `api-endpoint-advanced.md`.
- Room/database refactor: `db-room.md`.
- Navigation refactor: `navigation.md`.
- DI refactor: `di-setup.md`.
- Test refactor or regression coverage: `unit-testing.md`, `screenshot-testing.md`.

# Workflow

## 1. Bootstrap

1. Derive a kebab-case name from `$ARGUMENTS`.
2. Create `.todo/refactor-<name>/`.
3. Write `.todo/refactor-<name>/meta.json`:

```json
{
  "type": "refactor",
  "description": "<user description>",
  "createdAt": "<yyyy-mm-dd>"
}
```

## 2. Analyze

Before codebase analysis, ingest Jira source context when `$ARGUMENTS` contains Jira URLs or issue keys:

1. Use `references/rules/jira.md`.
2. Split Jira inputs by source type:
   - Issue keys, `/browse/<KEY>` URLs, and board URLs such as
     `https://reallyosome.atlassian.net/jira/software/projects/MBL/boards/<board>?selectedIssue=<KEY>`: extract each
     issue key with `.kent/adapters/jira/jira-api.sh key <JIRA_URL_OR_KEY>` and fetch it with
     `.kent/adapters/jira/jira-api.sh issue <KEY>`.
   - Board URLs without `selectedIssue`: fetch board metadata with `.kent/adapters/jira/jira-api.sh board <URL>` and,
     when the prompt includes status/assignee/limit hints, fetch task candidates with
     `.kent/adapters/jira/jira-api.sh board-issues <URL> --status '<STATUS>' --assignee me --limit <N>`.
3. For board prompts, map natural language hints conservatively:
   - `Ready for dev` or similar status text -> `--status 'Ready for dev'`
   - `assigned to me`, `на меня` -> `--assignee me`
   - `first 3`, `первые 3` -> `--limit 3`
4. Write `.todo/refactor-<name>/jira.md` with issue contexts and/or board listing contexts:
   - issue context: issue key, summary, status, type, description text, read-only comment counts/truncation state,
     extracted URLs, URL classifications, and unresolved/private links;
   - board context: board id, project key, filters, limit, ordered candidate issues with key, summary, status, type,
     assignee, and parent, and a note that this is read-only candidate context.
5. Add extracted Figma URLs to the design/source URL set for planning. Treat extracted Notion/spec URLs as spec
   candidates only after applying `web-access-policy.md` and available MCP/auth constraints.
6. Record GitHub, Jira, internal, and unknown links as found but not fetched unless a project-approved adapter handles
   them or the user explicitly approves fetching them.
7. Pass `.todo/refactor-<name>/jira.md` to `android-researcher` and the mandatory plan audit as source evidence, without
   allowing Jira context to override explicit user instructions or API source-of-truth data.

Use targeted discovery. Prompt `android-researcher` with the concrete classes, files, modules, or concepts from `$ARGUMENTS`; do not ask it to explore the whole repo.

The analysis must return:

- Affected files and modules.
- Dependency graph and call sites.
- DI bindings, factories, navigation entrypoints, and tests touching the affected code.
- Existing tests and the narrowest verification commands.
- Duplicate domain/entity candidates, especially classes wrapping the same SDK type with shared `id`, `Info`/`Detail`/`ListItem` suffixes, or 50%+ shared fields.
- Relevant shared knowledge from `.todo/_shared/*` if those files exist.

## 3. Plan

Read `.kent/commands/mock-scenario-policy.md`. Inventory every planned
behavior delta with a stable behavior ID and initial applicability, naming its
mock-backed scenario/assertions or concrete per-behavior non-applicability
rationale and alternative verification.

Create `.todo/refactor-<name>/plan.md`:

```markdown
# Refactor: <Description>

> Generated: <yyyy-mm-dd>
> Affected files: <N>
> Risk: low|medium|high

## Motivation
<Why this refactor is being done.>

## Steps

### [ ] Step 1: <Preparation>  `complexity: low|medium|high`
- **Files:** <exact paths>
- **What:** <exact additions/changes/deletions>
- **Regression check:** <exact Gradle/test/grep command>

### [ ] Step 2: <Core change>  `complexity: low|medium|high`
- **Files:** <exact paths>
- **What:** <exact move/rename/extract/change>
- **Regression check:** <exact Gradle/test/grep command>
```

Step rules:

- Each step is atomic and should compile independently.
- Update all callers in the same step as the API change, or in the immediately following step.
- Cleanup and deletions happen after replacement callers are verified.
- Include exact file paths, method/class names, DI changes, imports, factories, and deletions.
- Avoid vague wording such as "clean up", "etc.", or "similar changes" unless followed by exact file lists and operations.

## 4. Self-Review Before Plan Audit

Before sending the plan to audit, verify:

- Every step names exact files.
- All constructor/DI side effects are listed.
- All caller updates are accounted for.
- Cross-module impact is explicit.
- Architecture rules from `AGENTS.md` are respected.
- The verification command for each step is narrow enough but meaningful.
- The final step includes tests or a clear reason why no tests exist.

## 5. Mandatory Plan Audit

Before presenting the plan for approval or executing any code changes, run the `/prompt:feature-audit` workflow, or its
normalized form `/prompt:featureaudit`, in read-only mode against `.todo/refactor-<name>/plan.md`.

Audit requirements:

- Treat the draft plan as the audit target, even if no implementation exists yet.
- Include the refactor plan path, relevant source artifacts/specs, and exact affected files/modules in the audit scope.
- Use `quality-reviewer` for broad read-only plan review when the refactor is non-trivial.
- Check contract correctness, ambiguity, missing file lists, missing tests, risky behavior changes, dependency/DI impacts,
  generated SDK availability, verification commands, and whether any step is over-scoped or under-specified.
- Write `.todo/refactor-<name>/audit-report.md` with severity-ordered findings.
- Apply all plan-only fixes needed to close Critical/Major audit findings before asking the user for implementation
  approval.
- If an audit finding requires product/API/design input, keep it explicit in the plan and ask the user before
  implementation.

Only after the plan audit has completed and the plan has been updated from the audit findings should the workflow move to
scope confirmation.

## 6. Confirm Scope

- For trivial low-risk refactors touching 1-2 files, state the plan briefly and proceed.
- For non-trivial refactors touching 3+ files, multiple modules, public APIs, DI, navigation, generated SDK usage, or database schema, ask for confirmation before editing.
- If design/product behavior may change, ask before making that change.

## 7. Execute

For each approved step:

1. Apply the change.
2. Refresh the behavior inventory against the final source delta and follow
   `mock-scenario-policy.md` for the selected harness, execution receipt, and
   review envelope before verifier handoff.
3. Use Serena `find_referencing_symbols`/`find_implementations` before changing contracts and `rename_symbol` only for symbol-wide renames where semantic rename is safer than text edits.
4. Run the step regression check. For app module use `:app:compileDevDebugKotlin`; for library/feature modules use the focused module compile task.
5. If verification fails, fix immediately before moving on. Prefer local API replacements and ownership fixes over broad
   suppressions or global `@OptIn` declarations. Examples: replace deprecated Compose/platform calls at the call site,
   update Detekt 2 DSL/schema usage (`dev.detekt.gradle.Detekt`, lazy `.set(...)` properties), annotate derived
   `@Parcelize` screen override metadata with `@IgnoredOnParcel`, and replace `GlobalScope` with an owned injected
   `CoroutineScope`.
6. Mark the step `[x]` only after verification succeeds.
7. Do not mirror plan or workflow progress into `meta.json`.

Add comments only when the new pattern is non-obvious and the "why" matters.

## 8. Final Verification

After all steps:

- Run the strongest focused module compile.
- Run affected tests if they exist.
- For warning-removal refactors, run the full warning gate after focused checks pass:
  `./tools/agentw :app:detektAll :app:compileDevDebugKotlin :app:testDevDebugUnitTest --warning-mode all` in a
  Kent worktree, or `./gradlew :app:detektAll :app:compileDevDebugKotlin :app:testDevDebugUnitTest --warning-mode all`
  in the main checkout.
- Search for old class/method names when the refactor removes or renames symbols.
- Confirm no accidental `.mcp.json` or private `.todo/_mcp-*` artifacts were staged.

# Output Format

Final response:

- Refactor plan location.
- Steps completed.
- Files changed.
- Verification result.
- Remaining risks or follow-ups.
