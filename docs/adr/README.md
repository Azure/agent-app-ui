# Architecture decisions

Record Agent Landing Zone UI decisions here when they change a hard-to-reverse boundary,
contract, identity or session model, persistence strategy, accessibility
approach, deployment topology, or operational characteristic.

Use the `architecture` engineering agent and the `architecture-decision` skill
before implementation. Copy the template from
`.github/skills/architecture-decision/references/adr-template.md`, assign the
next `ADR-NNN` number, compare alternatives including no change, and define
measurable compliance and a review trigger.

These records cover this UI component. Platform decisions belong in
`Azure/agent-landing-zone`. Orchestrator-only decisions belong in
`Azure/agent-app-orchestrator`. Link to those decisions rather than
duplicating them here.

The exception-registry technical validation decision is platform ADR-0020,
`docs/adr/ADR-0020-technical-exception-validation.md` in
`Azure/agent-landing-zone`. It replaces mandatory independent approval and
registry-only policy blocking, while retaining protected evaluator integrity
and exact-source passing evidence. See
[UI quality checks](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/application-testing/#ui)
for the current implementation. Repository settings and live required-check
enforcement need separate evidence.
