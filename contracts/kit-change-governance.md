# Kit And Workflow Change Governance

This contract applies to changes requested for the Kent Engineering Kit,
generated or live workflows, workflow adapters/contracts, agent roles, and
Kent configuration. Ordinary product implementation follows its task workflow
and does not inherit this maintainer ceremony.

1. Investigate current behavior and affected surfaces.
2. Present a user-visible preview naming files, graph delta, rollout, rollback,
   and restart impact.
3. Obtain at least two independent read-only reviews of the preview. Reviewers
   do not edit files or mutate live Kent state.
4. Obtain explicit user approval.
5. Only then edit files or mutate live Kent state.

Approval is obtained once after reviews and before the first write. It covers
only the previewed scope. A material expansion stops mutation and requires a
revised preview, another read-only review pass, and new approval.
