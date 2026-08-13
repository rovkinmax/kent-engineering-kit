# Design Sources

The toolkit is original project infrastructure informed by:

- Kent's official documentation for global skills, prompts, role configuration,
  workflow graphs, script nodes, fan-out, Join, and project links.
- Kent 2.3 documentation and release notes for explicit workflow execution
  targets, server-managed worktree commands, and structured setup-hook payloads:
  https://github.com/respawn-llc/kent/releases/tag/v2.3.0
- Kent 2.3.0 source for persisted-ID or exact-name workflow resolution:
  https://github.com/respawn-llc/kent/blob/v2.3.0/cli/kent/workflow_command.go
- Kent 2.3.1 source for canonical bare-UUID workflow selectors:
  https://github.com/respawn-llc/kent/blob/v2.3.1/cli/kent/workflow_selector.go
- Kent 2.4 release notes and CLI help for required completion handoff,
  structural fan-out, labels, compact task output, workflow deletion preview,
  and runtime recovery fixes:
  https://github.com/respawn-llc/kent/releases/tag/v2.4.0
- Kent 2.4 model capability registry for exact GPT-5.6 model identifiers,
  context windows, reasoning levels, and verbosity support:
  https://github.com/respawn-llc/kent/blob/v2.4.0/server/llm/provider_factory.go
- Kent 2.5 release notes for workflow-wide transition keys, offset pagination,
  task continuity, and safe edits with existing tasks:
  https://github.com/respawn-llc/kent/releases/tag/v2.5.0
- Kent 2.5 workflow validation source for the global transition-key invariant:
  https://github.com/respawn-llc/kent/blob/v2.5.0/server/workflow/validation.go
- Kent 2.6.0 release notes for graph inspect and atomic graph apply, dynamic
  assignee/thinking selection, speculative
  compaction, task/run watch and wait, `kent question`, explicit
  `--branch-name`, worktree `base_dir` enforcement, and stderr-preserving
  recovery. Released August 12, 2026:
  https://github.com/respawn-llc/kent/releases/tag/v2.6.0
- Kent 2.6.1 release notes for frozen workflow provenance repair and immutable
  legacy-source proof. Released August 13, 2026:
  https://github.com/respawn-llc/kent/releases/tag/v2.6.1
- Kent 2.6.1 workflow graph CLI implementation for complete graph inspection
  and atomic graph application:
  https://github.com/respawn-llc/kent/blob/v2.6.1/cli/kent/workflow_command.go
- Kent workflow and task guide for graph lifecycle, task-backed revisions, and
  preview/apply operation:
  https://kent.sh/workflows/
- Kent worktree guide for the configured `worktrees.base_dir` namespace and
  source-workspace separation:
  https://kent.sh/worktrees/
- Kent headless guide for run observation, questions, and recovery-facing
  automation:
  https://kent.sh/headless/
- Kent configuration and headless-role references for model, reviewer,
  workflow concurrency, delegation depth, and role override keys:
  https://kent.sh/config.md
  https://kent.sh/headless.md
- Matt Pocock's `mattpocock/skills` repository for the separation between
  explicit orchestration and reusable engineering disciplines.
- MCPorter documentation and source for configured stdio servers, project/home
  config, and explicit ephemeral versus keep-alive lifecycle:
  https://github.com/openclaw/mcporter
- `claude-in-mobile` source for per-call mobile device addressing:
  https://github.com/AlexGladkov/claude-in-mobile
- Appsome and Puber Kent workflow experiments, including deterministic
  verification, worktree SDK bootstrap, conservative cleanup, device resource
  locking, and Kent 2.2 fan-out runtime constraints.

Project-specific source text and architecture recipes are not copied into the
global toolkit.
