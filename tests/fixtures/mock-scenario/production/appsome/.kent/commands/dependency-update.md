# Role and Objective

You are running `/prompt:dependency-update`. Check and optionally update project dependencies requested by `$ARGUMENTS` with a risk-prioritized plan and Gradle verification.

The desired outcome is either a clear update plan or approved dependency updates applied in safe batches.

# Inputs

- `$ARGUMENTS`: optional filter such as `compose`, `--security-only`, a dependency coordinate, or empty for all dependencies.
- `gradle/libs.versions.toml` and relevant `build.gradle.kts` files.
- Project repository declarations discovered from Gradle files.

# Kent Runtime Rules

- Follow `.kent/skills/appsome-android-workflow/SKILL.md`.
- Use Maven Central MCP only through `~/.kent/bin/kent-mcp-call` or `~/.kent/bin/kent-mcp-list`.
- Redirect the large response from command stdout and save the known-safe audit under
  `.todo/dependency-update-<date>/mcp`.
- If the MCP bridge is unavailable, report the blocker and ask before using web fallback.
- Use `build-doctor` for noisy compile failures.
- Do not commit or push unless explicitly requested.

# Recipe Loading

There is no dedicated dependency-update recipe. This command is the source of truth for the workflow. Still follow the skill's MCP bridge rules and use `build-doctor` for focused Gradle diagnostics when update failures are noisy.

# Workflow

## 1. Discover

1. Create `.todo/dependency-update-<yyyy-mm-dd>/`.
2. Read `gradle/libs.versions.toml` and relevant module build files.
3. Apply the optional filter from `$ARGUMENTS`.
4. Prefer MCP audit. Immediately before the persisted call, record
   `MCP_CALL_LOG_START` using the skill's persisted-response handoff:

```bash
~/.kent/bin/kent-mcp-call \
  maven-central.audit_project_dependencies \
  projectPath="$PWD" \
  includeVulnerabilities=true \
  --output json \
  --raw-dir ".todo/dependency-update-<yyyy-mm-dd>/mcp" \
  >/dev/null
```

5. Use the skill's persisted-response handoff with server `maven-central` and
   tool `audit_project_dependencies`. Read the exact `rawOutputPath` before
   classifying or planning updates.
6. If needed, query specific coordinates with Maven Central MCP. It supports Maven Central, Google Maven, Gradle Plugin Portal, and repositories discovered from Gradle/Maven files.

## 2. Classify Risk

Create a table of candidates:

```markdown
| Dependency | Current | Latest | Type | Risk | Notes |
|------------|---------|--------|------|------|-------|
| kotlin | 2.x.y | 2.x.z | patch | low | update with KSP |
```

Classification rules:

- Patch updates: lowest risk, can be batched.
- Minor updates: review changelog when library is UI/build/runtime critical.
- Major updates: one per step, require explicit approval.
- Kotlin and KSP must be updated together.
- Compose BOM updates are visual-risk changes and should be separate.
- AGP updates require checking build-logic convention plugins for deprecated APIs.
- `--security-only` means include security fixes and patch-level updates only.

## 3. Plan

Read `.kent/commands/mock-scenario-policy.md`. Classify the proposed final
behavior delta rather than exempting work by the dependency-update label;
inventory changed behaviors and their mock-backed scenarios or concrete
per-behavior non-applicability rationale and alternative verification.

Write `.todo/dependency-update-<yyyy-mm-dd>/plan.md`:

```markdown
# Dependency Update

> Date: <yyyy-mm-dd>
> Updates found: <N> (<P> patch, <M> minor, <K> major)

## Steps

### [ ] Step 1: Patch updates  `complexity: low`
- **Updates:** <list>
- **Files:** `gradle/libs.versions.toml`, <other exact files>
- **Verify:** `./gradlew :app:compileDevDebugKotlin` in the main checkout, or `./tools/agentw :app:compileDevDebugKotlin` in a Kent worktree

### [ ] Step 2: Compose BOM update  `complexity: medium`
- **Updates:** <list>
- **Visual risk:** yes
- **Verify:** compile plus relevant previews/Paparazzi if configured
```

Ask before applying updates unless `$ARGUMENTS` explicitly requests an approved bounded update.

## 4. Execute Approved Steps

For each approved step:

1. Update only the relevant version entries and build files.
2. Refresh the behavior inventory against the final source delta and follow
   `mock-scenario-policy.md` for the selected harness, execution receipt, and
   review envelope before verifier handoff.
3. Run the step verification command.
4. If compilation fails, diagnose API or build-logic changes and fix before continuing. Prefer adapting call sites to the
   new dependency API before adding broad global opt-ins, suppressions, or version rollbacks. For warning-producing
   upgrades, check for local replacements such as Detekt 2 DSL/schema updates (`dev.detekt.gradle.Detekt`, lazy
   `.set(...)` properties), Compose deprecation replacements, typed platform API branches, and project-owned components.
5. Mark the step `[x]` after verification succeeds.
6. Do not mirror plan or workflow progress into `meta.json`.

For dependency updates whose explicit goal is warning cleanup, finish with the same full warning gate used by refactors:
`./tools/agentw :app:detektAll :app:compileDevDebugKotlin :app:testDevDebugUnitTest --warning-mode all` in a Kent
worktree, or the same tasks through `./gradlew` in the main checkout. This gate complements, but does not replace,
focused module compiles for each update batch.

# Output Format

Final response:

- Updates found.
- Updates applied or skipped.
- Verification result.
- Major or risky updates deferred.
