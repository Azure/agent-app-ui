---
name: documentation-consistency
description: "Keeps Agent Landing Zone UI developer, user, and operator documentation aligned with shipped behavior. Use for features, configuration keys, security policy, deployment parameters, defaults, UI behavior, or breaking changes."
---

# Agent Landing Zone UI documentation consistency

All application user, operator, and developer manuals are published from
[`Azure/AI-Landing-Zones`, `main`, `docs/agent-landing-zone`](https://github.com/Azure/AI-Landing-Zones/tree/main/docs/agent-landing-zone)
at https://azure.github.io/AI-Landing-Zones/agent-landing-zone/.

This repository keeps concise entry-point links, internal contributor rules,
governance, security policies, ADRs, contracts, code comments, source examples,
and release metadata. Keep runtime ownership local. `chainlit.md` is an
intentional runtime presentation asset even when empty, not a product-manual
destination. Preserve it alongside `public/`, `.chainlit/`, and `VERSION`.

1. Identify the developer, user, or operator behavior that changed.
2. Search local and central documentation for the feature, configuration key,
   endpoint, security requirement, UI term, and previous behavior.
3. Update affected internal engineering guidance and entry-point links locally.
   Do not add local product manuals or redirect-guide stubs.
4. Update affected application manuals in `Azure/AI-Landing-Zones` on `main`.
   Coordinate that change when the task does not authorize central edits.
5. Keep the repository README concise and link to canonical documentation
   instead of duplicating application or platform guidance.
6. Ensure examples match current defaults, supported auth modes, security
   boundaries, deployment modes, and accessibility behavior.
7. Report local and central documentation status in the implementation
   handoff.

A user-visible or operator-visible change is incomplete until documentation is
updated or the search demonstrates that no published page is affected.
