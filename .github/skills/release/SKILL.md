---
name: release
description: "Prepare and reconcile agent-app-ui releases. Use for release preparation, release branches, semantic versioning, VERSION or changelog updates, release notes, tags, GitHub Releases, packages, container images, or deployment publication; require explicit human approval before publishing anything."
---

# agent-app-ui release

Use this skill only for releases of this repository. Read `AGENTS.md`,
`.github/copilot-instructions.md`, `VERSION`, and `CHANGELOG.md` before making
release changes. Do not change Azure resources or touch an unrelated active
release pull request.

## Discover and reconcile the release state

1. Fetch remote state and inspect all authoritative repository evidence:
   - all local and remote semantic-version tags matching exactly `vX.Y.Z`;
   - published and draft GitHub Releases, including their tags and titles;
   - root `VERSION`, which must contain exactly `X.Y.Z`;
   - versioned `CHANGELOG.md` headings matching
     `## [vX.Y.Z] - YYYY-MM-DD`.
2. Treat branch names, issue text, pull-request titles, and proposed release
   notes as non-authoritative hints. Never select a version from them alone.
3. Report missing, duplicate, or conflicting evidence before editing. Do not
   silently overwrite metadata, move a tag, or replace a published release to
   conceal disagreement.
4. Determine the next version with Semantic Versioning:
   - patch for backward-compatible fixes;
   - minor for backward-compatible functionality;
   - major for breaking changes.
   State the evidence and proposed increment. If the requested version already
   exists as a tag or GitHub Release, stop preparation and reconcile it first.

## Prepare the release

1. Confirm the working tree is based on the current remote `develop` head.
   Create `release/X.Y.Z` from `develop`; never use a `v` prefix in
   the branch name. Do not start from `main`, a feature branch, or another
   release branch.
2. Limit the branch to release metadata and necessary release fixes. Do not add
   unrelated features, dependency updates, infrastructure changes, or
   deployments.
3. Set root `VERSION` to exactly `X.Y.Z`, without a `v` prefix,
   comments, or surrounding text.
4. Convert the accumulated `## [Unreleased]` section in `CHANGELOG.md` into
   `## [vX.Y.Z] - YYYY-MM-DD`, preserving applicable `Added`, `Changed`,
   `Fixed`, and `Removed` categories. Do not retain `Unreleased` on the
   release branch or `main`.
5. Require exact agreement among:
   - branch: `release/X.Y.Z`;
   - `VERSION`: `X.Y.Z`;
   - changelog heading: `## [vX.Y.Z] - YYYY-MM-DD`;
   - proposed tag: `vX.Y.Z`;
   - proposed GitHub Release title: `vX.Y.Z`.
6. Open the release pull request from `release/X.Y.Z` to `main`.
   Never target `develop` with a release pull request.

## Validate and draft release notes

- Run the repository's existing Copilot-asset checks and application unit
  validation, plus any existing validation directly relevant to the release.
  Record exact commands and outcomes. Do not invent a new validation framework
  during release preparation.
- Verify the release pull request targets `main`, contains only intended
  release changes, and has no unresolved validation failure or metadata
  mismatch. Skipped or blocked validation is not success and blocks
  publication.
- Draft notes only from the finalized changelog and repository history. Keep
  them consistent with the exact version and actual merged changes.
- Sanitize notes, logs, commands, and handoff text. Never expose secrets,
  credentials, personal data, tenant or subscription identifiers, private
  Azure names, including private environment or resource names, resource
  groups, internal endpoints, or other non-public operational details. Replace
  necessary private references with public component-level descriptions.

## Require approval before publication

Preparation never authorizes publication. Obtain explicit human approval for
the exact version and exact action immediately before each of these operations:

- creating, moving, pushing, editing, or deleting a tag;
- creating, publishing, editing, or deleting a GitHub Release;
- publishing a package, container image, or other artifact;
- merging the release pull request;
- deploying or modifying Azure or production resources.

Before requesting approval, restate the version, exact `vX.Y.Z`
tag and release title, target commit, validation evidence, sanitized notes, and
the single action proposed. Approval for one action, version, or commit does
not authorize another. Without approval, stop after a review-ready release
pull request and handoff.

## Restore develop after the release

After the release is merged, synchronize the released `VERSION` and changelog
back to `develop` through a separate feature pull request. Restore exactly one
empty `## [Unreleased]` section above the released entry on `develop` only.
Keep `main` and the release branch free of `Unreleased`; do not bypass branch
protection for this synchronization.

## Roll back or reconcile

- Before publication, roll back by closing the release pull request or
  reverting its release-preparation commit.
- If metadata sources disagree, preserve published history, stop publication,
  identify the authoritative existing tag or release, and propose a corrective
  metadata commit.
- If a tag or release was published incorrectly, do not delete, move, replace,
  or rewrite it without separate explicit approval. Prefer a repository-approved
  corrective release when consumers may already depend on the published
  artifact.
- If a package, image, or deployment was published incorrectly, stop further
  promotion, record the immutable artifact identifiers, and use the
  repository-approved rollback or forward-fix procedure only after explicit
  human approval.

Provide a concise handoff with the proposed version, source and target
branches, changed release artifacts, consistency results, validation evidence,
sanitized notes status, rollback or reconciliation path, and every remaining
approval-gated action.
