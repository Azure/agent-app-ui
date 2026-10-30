---
name: component-release
description: "Prepares a verifiable Agent Landing Zone UI release. Use for release branches, VERSION, changelog entries, tags, release notes, and compatibility evidence; never publish without explicit human approval."
---

# Agent Landing Zone UI component release

1. Start from the current `develop` branch and create `release/X.Y.Z`.
2. Choose a semantic version from the shipped change set. Do not change
   `VERSION` on `develop` merely to anticipate a release.
3. Set root `VERSION` to `X.Y.Z` without a `v` prefix.
4. Convert the accumulated `## [Unreleased]` section into
   `## [vX.Y.Z] - YYYY-MM-DD`. Do not retain `Unreleased` on a release branch
   or `main`.
5. Verify the branch name, `VERSION`, changelog heading, proposed tag
   `vX.Y.Z`, and release title describe the same version.
6. Run the complete existing test suite and any auth, accessibility,
   deployment, or cross-repository validation required by the changes.
7. Confirm documentation, migration, compatibility, and rollback status.
8. Open the release pull request from `release/X.Y.Z` to `main`.
9. After merge and explicit human approval, create the tag and release using
   the validated commit. Publishing images or deploying production requires
   separate explicit approval.
10. After merge, synchronize the released version and changelog to `develop`
    through a separate feature pull request. Restore exactly one empty
    `## [Unreleased]` section on `develop` only, without bypassing protection.
11. Record the release artifacts, evidence, compatibility assumptions, and
    rollback path in the handoff.

Do not introduce unrelated feature work on a release branch. Never include
credentials, personal data, private environment names, tenant or subscription
identifiers, or internal endpoints in release artifacts.
