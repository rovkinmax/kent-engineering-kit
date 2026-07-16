# Design Sources

The toolkit is original project infrastructure informed by:

- Kent's official documentation for global skills, prompts, role configuration,
  workflow graphs, script nodes, fan-out, Join, and project links.
- Kent 2.3 documentation and release notes for explicit workflow execution
  targets, server-managed worktree commands, and structured setup-hook payloads:
  https://github.com/respawn-llc/kent/releases/tag/v2.3.0
- Matt Pocock's `mattpocock/skills` repository for the separation between
  explicit orchestration and reusable engineering disciplines.
- Appsome and Puber Kent workflow experiments, including deterministic
  verification, worktree SDK bootstrap, conservative cleanup, device resource
  locking, and Kent 2.2 fan-out runtime constraints.

Project-specific source text and architecture recipes are not copied into the
global toolkit.
