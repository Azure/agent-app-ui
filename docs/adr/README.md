# Architecture decisions

Record Agent Landing Zone UI decisions here when they change a hard-to-reverse boundary,
contract, identity or session model, persistence strategy, accessibility
approach, deployment topology, or operational characteristic.

Use the `architecture` engineering agent and the `architecture-decision` skill
before implementation. Copy the template from
`.github/skills/architecture-decision/references/adr-template.md`, assign the
next `ADR-NNN` number, compare alternatives including no change, and define
measurable compliance and a review trigger.

These records cover this UI component. Platform or orchestrator decisions
belong in the appropriate `Azure/agent-landing-zone` repository and should be linked
rather than duplicated.

The exception-registry technical validation decision is platform ADR-0020,
`docs/adr/ADR-0020-technical-exception-validation.md` in
`Azure/agent-landing-zone`. It replaces mandatory independent approval and
registry-only policy blocking, while retaining protected evaluator integrity
and exact-source passing evidence. See `../python-development.md` for the
implementation and pending protected-policy adoption.
