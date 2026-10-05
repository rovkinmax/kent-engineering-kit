# Role and Objective

You are running `/prompt:migration-start`. Plan and execute a codebase migration described by `$ARGUMENTS` in dependency order.

The desired outcome is a migration workspace, a leaf-first plan, and verified incremental changes that never intentionally leave the project broken.

# Inputs

- `$ARGUMENTS`: free-text migration description and scope.
- Existing codebase, Gradle modules, version catalog, convention plugins, and tests.

# Kent Runtime Rules

- Follow `.kent/skills/appsome-android-workflow/SKILL.md`.
- Use `kent run --agent android-researcher --workspace "$PWD" "<prompt>"` for bounded read-only migration analysis.
- Use `kent run --agent build-doctor --workspace "$PWD" "<prompt>"` for compile failure diagnosis.
- Use `~/.kent/bin/kent-mcp-call` and `~/.kent/bin/kent-mcp-list` for MCP access; do not use raw `mcp__...` tool names.
- Use Serena for semantic symbol discovery, references, implementations, and rare symbol-wide renames. Use Kent `patch` for local edits.
- Do not use `jetbrains.build_project`; use Gradle for build verification.
- Do not commit or push unless explicitly requested.

# Recipe Loading

There is no single dedicated migration recipe. During planning/execution, load only recipes matching each migration step:

- UI/Compose migration: `compose-screen.md`, `ui-components.md`, `compose-previews.md`, `compose-performance.md`.
- Navigation migration: `navigation.md`.
- DI migration: `di-setup.md`.
- Data/API migration: `api-endpoint.md`, `api-endpoint-advanced.md` when status changes, PATCH/null semantics, or caching are involved.
- Room/database migration: `db-room.md`, `domain-mapper.md`.
- ViewModel migration: `viewmodel.md`, `viewmodel-advanced.md`, `error-handling.md`.
- Test migration: `unit-testing.md`, `screenshot-testing.md`.

# Workflow

## 1. Bootstrap

1. Derive a kebab-case name from `$ARGUMENTS`.
2. Create `.todo/migration-<name>/`.
3. Write `.todo/migration-<name>/meta.json`:

```json
{
  "type": "migration",
  "description": "<user description>",
  "createdAt": "<yyyy-mm-dd>"
}
```

## 2. Analyze

Use `android-researcher` with a narrow prompt based on the requested migration.

The analysis must identify:

- Files and modules in scope.
- Dependency graph ordered from leaf modules/files to dependents.
- Call sites, imports, DI bindings, navigation entrypoints, tests, and generated code boundaries.
- Required version catalog, build-logic, or Gradle convention plugin changes.
- Compatibility risks and breaking API changes.
- The narrowest compile/test command for each module.

## 3. Plan

Read `.kent/commands/mock-scenario-policy.md`. Inventory each planned
behavior delta with a stable behavior ID and initial applicability, naming its
mock-backed scenario/assertions or concrete per-behavior non-applicability
rationale and alternative verification.

Generate `.todo/migration-<name>/plan.md`:

```markdown
# Migration: <Description>

> Generated: <yyyy-mm-dd>
> Scope: <N> files in <M> modules
> Dependency order: leaf-first
> Risk: low|medium|high

## Pre-migration Checklist
- [ ] <dependency/version/build-logic prerequisite>

## Steps

### [ ] Step 1: <Leaf module/file>  `complexity: low|medium|high`
- **Files:** <exact paths>
- **What:** <exact migration work>
- **Depends on:** none
- **Breaking changes:** <none or exact list>
- **Verify:** <exact command>

### [ ] Step 2: <Dependent module/file>  `complexity: low|medium|high`
- **Files:** <exact paths>
- **What:** <exact caller/import/API updates>
- **Depends on:** Step 1
- **Verify:** <exact command>
```

Planning rules:

- Leaf modules/files first, dependents after.
- Add new dependencies before migrating callers; remove old dependencies last.
- Never create a step that knowingly breaks compilation until a later step.
- Include exact files and symbols; avoid vague "cleanup" steps without file lists.
- Final step includes full focused compile and tests for affected modules.

## 4. Self-Review and Confirmation

Before editing:

- Check every step has exact paths and verification commands.
- Check side effects: imports, DI, factories, generated SDK usage, database schema, Gradle files, deleted files.
- For large/risky migrations, 10+ files, multiple modules, public API changes, build-logic changes, or dependency major upgrades, present the plan and ask for confirmation.
- If there are open design/product decisions, ask before editing.

## 5. Execute

For each approved step:

1. Apply the migration change.
2. Refresh the behavior inventory against the final source delta and follow
   `mock-scenario-policy.md` for the selected harness, execution receipt, and
   review envelope before verifier handoff.
3. Use Serena `find_referencing_symbols`/`find_implementations` before changing contracts and `rename_symbol` only for symbol-wide renames where semantic rename is safer than text edits.
4. Run the step verification command.
5. If verification fails, fix immediately before continuing.
6. Mark the step `[x]` only after verification succeeds.
7. Do not mirror step or workflow progress into `meta.json`.

## 6. Migration Patterns

- XML to Compose: migrate one screen at a time, keep navigation via `OsomeScreen`, and remove legacy fragments only after Compose replacement is wired and verified.
- Library replacement: add the new dependency alongside the old one, migrate callers one by one, remove the old dependency last, and watch for transitive dependency conflicts.
- API/SDK migration: generated SDK sources are source of truth. Follow the KMP SDK rules in `AGENTS.md` before concluding a type or field is missing.
- Gradle/build migration: preserve configuration cache and build cache. Use convention plugins; do not add `allprojects {}` or `subprojects {}`.

# Output Format

Final response:

- Migration plan location.
- Modules/files migrated.
- Verification result.
- Old dependencies or obsolete code removed: yes/no.
- Remaining risks or manual follow-ups.
