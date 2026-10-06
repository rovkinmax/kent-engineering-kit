# Role and Objective

You are running `/prompt:test-coverage`. Analyze and improve test coverage for the module, package, feature, or files described by `$ARGUMENTS`.

The desired outcome is a prioritized coverage map and, when approved, focused tests that follow project patterns and pass.

# Inputs

- `$ARGUMENTS`: required target path, module, feature, package, or class name.
- Existing production code and test source sets.
- Unit testing recipe: `.kent/skills/appsome-android-workflow/references/recipes/unit-testing.md`.
- Screenshot/Paparazzi recipe when UI component coverage is relevant: `.kent/skills/appsome-android-workflow/references/recipes/screenshot-testing.md`.

# Kent Runtime Rules

- Follow `.kent/skills/appsome-android-workflow/SKILL.md`.
- Use `android-researcher` for coverage mapping when the scope is broader than a few files.
- Use `build-doctor` for noisy test/compile failures.
- Use Serena for symbol overviews and references when mapping production code to existing tests.
- Do not commit or push unless explicitly requested.

# Recipe Loading

Recipe directory: `.kent/skills/appsome-android-workflow/references/recipes/`.

- Always load `unit-testing.md` before writing unit tests.
- Load `screenshot-testing.md` only for Paparazzi/component visual coverage.
- Load source-area recipes only when needed to understand the production pattern being tested, such as `viewmodel.md`, `api-endpoint.md`, `domain-mapper.md`, or `db-room.md`.

# Workflow

## 1. Bootstrap

1. Validate `$ARGUMENTS`. If missing, ask for the target module/path.
2. Create `.todo/test-coverage-<name>/`.
3. Write `.todo/test-coverage-<name>/meta.json`:

```json
{
  "type": "test-coverage",
  "target": "<path-or-module>",
  "createdAt": "<yyyy-mm-dd>"
}
```

## 2. Analyze

Map production files to existing tests:

- Include ViewModels, interactors, repositories, mappers, validators, reducers, and non-trivial domain logic.
- Skip trivial data classes, constants, and Screen DI wrappers.
- For component files, check whether previews or Paparazzi coverage exists instead of forcing unit tests.
- Identify source file, existing test file, gap, risk, and proposed cases.

Use `android-researcher` for broad scopes and request a compact table, not a narrative.

## 3. Plan

Read `.kent/commands/mock-scenario-policy.md`. Classify the behavior delta
covered by the tests, not the test-only work kind; inventory applicable
behavior/scenario assertions or concrete per-behavior non-applicability and
alternative verification.

Write `.todo/test-coverage-<name>/plan.md`:

```markdown
# Test Coverage: <Target>

> Generated: <yyyy-mm-dd>
> Source files: <N>
> Already tested: <M>
> To add: <P> test files

## Coverage Map

| Source File | Test File | Status | Priority |
|-------------|-----------|--------|----------|
| <path> | <path or -> | missing|exists | high|medium|low |

## Implementation Steps

### [ ] Step 1: <Class> tests  `priority: high`
- **Source:** <exact path>
- **Create/modify:** <exact test path>
- **Cases:** <explicit cases>
- **Verify:** `./gradlew :<module>:testDebugUnitTest --tests "<TestClassName>"` in the main checkout, or `./tools/agentw :<module>:testDebugUnitTest --tests "<TestClassName>"` in a Kent worktree
```

Ask before adding a large number of tests or snapshot/Paparazzi coverage.

## 4. Execute Approved Test Steps

For each approved step:

1. Load `unit-testing.md`; also load `screenshot-testing.md` for Paparazzi/component coverage.
2. Read the source file and nearby tests to follow local patterns.
3. Write tests with explicit expected values and meaningful edge cases.
4. Prefer regression tests that would fail on the old buggy behavior.
5. Avoid locale-dependent assertions for dates, currency, and numbers. Assert structural properties instead.
6. Refresh the behavior inventory against the final source delta and follow
   `mock-scenario-policy.md` for the selected harness, execution receipt, and
   review envelope before verifier handoff.
7. Run the focused test command.
8. Fix failures before moving on.
9. Mark the plan step `[x]` only after verification succeeds.

# Test Quality Rules

- Do not unit-test trivial data classes.
- Do not unit-test Screen DI wrappers.
- Use real project patterns such as MockK, `runTest`, Turbine, and existing base test utilities.
- Mapper edge cases should cover empty input, single item, null/missing fields, invalid format input, item IDs/content, and ordering/structure when relevant.
- Count-only assertions are weak; prefer explicit expected values that explain failures.

# Output Format

Final response:

- Coverage gaps found.
- Tests added or planned.
- Verification result.
- Deferred coverage items.
