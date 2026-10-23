# Waiting PR

Use the profile-owned PR watcher and generated Waiting PR/Merge Watch
transitions. Observe the exact PR and head, feedback and merge state; never
merge it, force-push main or substitute a separate CI monitoring stage.
Repository check policy still applies.

When `pr_base_branch` is declared, compare every fresh GitHub `baseRefName`
with that exact carried target before reporting ready, merged or cleanup
success. A target change requires applicable existing owner authority and a
fresh PR observation. When authorized, use `post_pr_target_revalidated` to
change only the target in the v2 packet; preserve CI packets and retry state.
This packer mode adds no graph edge or authority. Never infer a target from
the task branch, repository default or watcher output.

Pending checks or merge time is waiting, not a new user decision. Route
actionable task-scoped feedback to Fix, preserving authority and baseline;
require refreshed evidence after source changes. Ask only when an actual
external action or material scope decision is required.

Record verified merged delivery, exact PR/head/base and retained evidence
before Cleanup. After merged delivery, an explicitly authorized tracking-only
fetch/preflight may prepare future starts; never checkout or fast-forward
the installed primary main. Do not treat a stale local origin/HEAD as fresh
published-source evidence.
