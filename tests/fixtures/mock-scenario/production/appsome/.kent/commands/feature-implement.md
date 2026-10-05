# Role and Objective

You are running `/prompt:feature-implement`. Implement the next or requested step from an explicit feature plan.

The desired outcome is one completed plan step, verified code, and updated `.todo/<feature>/plan.md`.

# Inputs

- `$ARGUMENTS`: optional step number or implementation instruction.
- Explicit feature name or `.todo/<feature>` workspace path in `$ARGUMENTS` or Kent workflow task context.
- Resolved `.todo/<feature>/meta.json`, `plan.md`, `spec.md`, design artifacts, and optional iOS reference.
- Recipes under `.kent/skills/appsome-android-workflow/references/recipes/`.

# Kent Runtime Rules

- Follow `.kent/skills/appsome-android-workflow/SKILL.md`.
- Read `.kent/commands/mock-scenario-policy.md` for every implementation step.
- Load `.kent/skills/appsome-android-workflow/references/rules/feature-target-resolution.md` before selecting the plan.
- Load `.kent/skills/appsome-android-workflow/references/recipes/feature-context.md` first.
- Load only recipes relevant to the selected step.
- Load `.kent/skills/appsome-android-workflow/references/rules/serena.md` when the step modifies existing Kotlin symbols, contracts, references, or shared module APIs.
- Load `.kent/skills/appsome-android-workflow/references/rules/jira.md` when resolved feature artifacts or the selected
  step mention Jira issue keys, Jira URLs, or ticket metadata.
- In a Kent workflow, the assigned Implement/Fix agent owns the bounded
  implementation slice, verification, and `plan.md` progress; do not create a
  nested implementation writer or overlapping write.
- In standalone manual orchestration outside a workflow, a caller may assign
  one bounded slice to an `implementation-worker` with explicit file ownership
  when the effective role and configuration contract permits it. The parent
  must not write concurrently to worker-owned files and owns integration and
  progress after the worker returns.
- Do not use `jetbrains.build_project`; use Gradle for build verification.
- Do not commit or push unless explicitly requested.

# Recipe Loading

Recipe directory: `.kent/skills/appsome-android-workflow/references/recipes/`.

Always load `feature-context.md` first. Then load only recipes selected by the step type/trigger table below. Do not load all recipes.

# Workflow

## 1. Select Step

1. Resolve the feature workspace using `feature-target-resolution.md`. If the target is missing or ambiguous, ask for the `.todo/<feature>` path/name.
2. Read `meta.json` only for identity and source/artifact metadata, then read `plan.md`.
3. If `$ARGUMENTS` names a step number, select that step.
4. Otherwise select the first unchecked `[ ]` step whose dependencies are complete.
5. Verify all `Depends on:` steps are completed.
6. If all steps are complete:
   - In a Kent workflow task, complete through the workflow's `audit` transition and provide `workspace_path` plus
     `plan_path`; do not invoke a nested `/prompt:*` flow.
   - In manual command use, recommend `/prompt:feature-audit <workspace>` without invoking it.

## 2. Load Relevant Context

Use the recipes selected above and load them before editing code.

If `$ARGUMENTS` or the selected step contains Jira URLs or issue keys that are not already captured in
`.todo/<feature>/jira.md`, use `references/rules/jira.md` and refresh read-only Jira source context before editing:

- For issue keys, `/browse/<KEY>` URLs, and board URLs with `selectedIssue=<KEY>`, run
  `.kent/adapters/jira/jira-api.sh issue <KEY>` and append/update issue metadata, description text, comment
  count/truncation state, extracted URLs, and unresolved/private links.
- For board URLs without `selectedIssue`, run `.kent/adapters/jira/jira-api.sh board <URL>` and, when the prompt or
  selected step includes status/assignee/limit hints, run
  `.kent/adapters/jira/jira-api.sh board-issues <URL> --status '<STATUS>' --assignee me --limit <N>`. Append/update
  board id, project key, filters, limit, ordered candidates with key/summary/status/type/assignee/parent, and mark it as
  read-only candidate context.

If Jira auth is not available, record the missing Jira context as a blocker instead of inventing ticket details.

Recipe selection:

| Step type or trigger | Recipes |
|----------------------|---------|
| `screen`, UI | `compose-screen.md`, `ui-components.md`, `compose-previews.md`, `compose-performance.md` |
| list, paging | screen recipes plus `paging-list.md` |
| filters or pickers | screen recipes plus `filtering.md` |
| search | screen recipes plus `paging-list.md`, `viewmodel.md`, `compose-screen-advanced.md`, `viewmodel-advanced.md` |
| `viewmodel`, VM | `viewmodel.md`, `error-handling.md` |
| `data`, `api`, repository, interactor | `api-endpoint.md` |
| Room, DAO, table, db | `db-room.md`, `domain-mapper.md` when mappings are added |
| navigation, flow | `navigation.md` |
| DI, module | `di-setup.md` |
| tests | `unit-testing.md`, `screenshot-testing.md` as applicable |
| form, draft, create/edit, auto-save | `form-screen.md`, `viewmodel-advanced.md` |

Load advanced recipes only when the step explicitly needs them.

For non-UI steps, skip heavy design screenshots unless the step references UI behavior.

## 3. Implement

Implement exactly what the selected step specifies:

- Implement one plan step per invocation.
- In a Kent workflow, do not delegate the write to another implementation
  worker; only bounded read-only research or build diagnosis may be delegated.
- In standalone manual orchestration, delegate only when the slice has explicit
  file boundaries and does not need ownership of shared integration files.
- Follow project architecture from `AGENTS.md`.
- Use Serena for symbol overviews/reference checks before changing existing Actions, ViewStates, Params, navigation screens, DI bindings, interactor/repository interfaces, entities, or mapper methods.
- Prefer Kent `patch` for local edits; use Serena semantic tools for reference discovery and rare symbol-wide renames.
- Keep composables VM-free: pass state and `onAction`.
- Put user-visible strings in resources.
- Use generated KMP SDK sources as source of truth for API models.
- Reuse existing module patterns before adding dependencies.
- Ask before ambiguous UI/product decisions.

## 4. Verify

Use the narrowest meaningful verification:

- App module: `./gradlew :app:compileDevDebugKotlin` in the main checkout, or `./tools/agentw :app:compileDevDebugKotlin` in a Kent worktree.
- Feature/library module: `./gradlew :feature:<name>:compileDebugKotlin` in the main checkout, or `./tools/agentw :feature:<name>:compileDebugKotlin` in a Kent worktree. Use the exact module compile task when it differs.
- Tests: focused `testDebugUnitTest --tests "<TestClassName>"` when tests were added or changed.

Before verifier handoff, refresh the behavior inventory against the final source
delta and follow `mock-scenario-policy.md` to capture execution evidence and
serialize its review envelope. Existing test execution alone is not scenario
evidence; Fix changes require a refreshed packet and receipt.

For screen steps:

- Add or update previews for relevant ViewState variants.
- If Paparazzi is configured and visual fidelity matters, generate snapshots for comparison without treating them as golden updates unless requested.
- Check system padding, hardcoded strings, stable list keys, loading state, and duplicate screen/component risks.

For ViewModel/domain/data steps:

- Check action exhaustiveness, error handling, DI registration, mapper completeness, and API nullability.
- Use Serena `find_referencing_symbols`/`find_implementations` when verification or the plan suggests contract changes may affect existing callers.

If verification fails, fix before updating the plan.

## 5. Update Progress

After successful verification:

1. Mark the step `[x]` in `.todo/<feature>/plan.md`.
2. Do not mirror step or workflow progress into `meta.json`.
3. Save reusable discoveries to `.todo/_shared/*` only when they are genuinely reusable.

# Output Format

Final response:

- Step completed.
- Files created/modified.
- Verification result.
- Next step.
- Any deferred decisions or smoke-test recommendation.
