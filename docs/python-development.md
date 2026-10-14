# Python development and package migration

This is the UI implementation checkpoint for
[Azure/agent-landing-zone#681](https://github.com/Azure/agent-landing-zone/issues/681), coordinated in
[Azure/agent-landing-zone#689](https://github.com/Azure/agent-landing-zone/pull/689).
The accepted coordination ADR is ADR-0005 in that PR. The component remains
compatible by design with orchestrator v4.1.1 and ingestion v2.7.3; local
mocked-boundary evidence is not live Azure integration evidence.

## Setup and unchanged startup

Use Python 3.12:

```text
python -m pip install -r requirements.txt
python -m pip install -r requirements-quality.txt
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
uvicorn main:app --host 0.0.0.0 --port 8080
```

`requirements.txt` remains the sole runtime dependency list. The setuptools
distribution installs code and supported legacy modules, with no duplicate
runtime dependency declaration. Editable installation is for contributors;
deployment uses `python -m pip install --no-deps .`. Docker still runs from
`/app` with the same Uvicorn command. No deployment hooks, infrastructure,
backend wire contracts, authentication defaults or frontend assets change.
`VERSION` is deliberately unchanged, including its existing value.

Expected dependency failures retain their established UI outcomes. Missing
configuration uses its declared default immediately; Azure provider failures
use the existing bounded retries and an observable default on exhaustion.
`config.errors.ConfigurationError` identifies a missing required setting or
invalid conversion. Unexpected provider defects (including unrelated nested
`RetryError`) propagate instead of becoming a missing setting. The retry
notification is a static callback, fixing the previously masked unbound-method
failure on the first retry.

Conversation HTTP/JSON failures retain empty/absent/false results, while
unexpected implementation errors propagate. Blob, diagnostic JWT, VERSION and
optional logging boundaries catch their documented dependency/validation
failures rather than arbitrary defects. Existing standalone download, chat,
feedback, socket invalidation and optional citation/panel-hook outcomes remain
explicit application boundaries; exception metadata alone is not approval authority.

## Ownership and imports

`src/gpt_rag_ui/bootstrap.py` owns ASGI composition and the application instance.
`api/` owns route/callback registration; `services/` owns chat, shared citation
rendering, history, continuity, conversation/download decisions and panel
metadata operations. `auth/` owns identity/session/transport primitives;
`clients/` owns backend/storage transports. `config/` owns the existing
AppConfig provider and cache, settings and resource-root resolution;
`telemetry/` owns instrumentation; `util/` owns constants.

`.quality/migration.json` records all 32 researched runtime modules and original
public exports against `c635bc6696714b543feec24b4a062a8a8f3ff6d0`.
`.quality/policy.json` lists current canonical modules and explicit legacy
adapters. Root `main.py`, `app.py`, the other legacy modules and `connectors/`
contain no business definitions. Public callables/classes share their canonical
objects. Tests patch canonical owners, not arbitrary private adapter globals.

History's split is explicit: `api.history.OrchestratorDataLayer` implements
Chainlit callbacks and `get_data_layer()` still creates a fresh adapter per
call. `services.history.HistoryService` owns history/user operations and the
single `_users` cache. `HistoryOperationContext` carries authenticated metadata
or the request Copilot session. Only the API resolves ambient session state,
owns consume-once request metadata and updates the selected conversation.
The service checks ownership before invoking that API selection callback and
resolving the rename token. No framework-free DTO hierarchy or history storage
rewrite is introduced. The adapter preserves the existing `None` denial result
for `get_thread_author`, despite Chainlit's narrower `str` annotation.

Dependencies flow from bootstrap to API, services, clients, auth, telemetry,
config and util, with no upward edges or cycles. Implementations never import
legacy adapters. Private first-party imports require exact recorded allowances;
the five existing ingestion-to-orchestrator transport helper imports are
explicitly inventoried within `clients/`, not exported as a new shared API.

`api.callbacks.register_callbacks()`, `api.history.register_data_layer()` and
`api.oauth.register_oauth_callback()` own idempotent registration. The OAuth
adapter accepts Chainlit 2.12.0's optional fifth argument and delegates to the
unchanged four-argument OAuth implementation. Classic backend startup does not
import hosted Conversations or panel Cosmos. `hosted_agent` remains the default.

## Installed resources

The supported installed artifact is **code plus staged assets**, not a
self-contained wheel. Keep `.chainlit/`, `public/`, `chainlit.config.yaml`,
`chainlit.md` and `VERSION` together in the writable application root.
Use existing `CHAINLIT_APP_ROOT` for an explicitly staged root, or start from
that directory. A source-checkout `main.py` passes its adjacent asset root.
The root is resolved before importing Chainlit and is shared by VERSION,
config synchronization and Chainlit public/upload paths. Never write to
site-packages or introduce `sys.path`/`sys.modules` compatibility tricks.

Missing VERSION remains optional; missing `.chainlit/config.toml` retains the
existing warning behavior. Disconnected App Configuration remains a 503
not-ready application rather than a successful chat fallback.

```text
python -m pip wheel --no-deps --wheel-dir .artifacts/wheel .
python -m unittest discover -s tests -p test_module_compatibility.py -v
python -m unittest discover -s tests -p test_installed_package.py -v
```

Installed acceptance uses temporary non-repository directories and a
non-editable wheel. It creates a clean venv, installs the unchanged
`requirements.txt`, runs `pip check`, and asserts canonical and legacy UI
imports resolve from that environment. It also copies the existing behavioral
tests, not application sources, and runs them against the installed wheel.
This clean dependency/installed behavior coverage supersedes the earlier
system-site-packages checkpoint; it is not itself Linux image evidence.

## Quality interface and current adoption limits

```text
python .github/scripts/check-quality.py --check all --base-ref <protected-base-sha> --report .artifacts/quality.json
python -m unittest discover -s tests -p test_quality_policy.py -v
```

`--check` accepts `all`, `lint`, `typing`, `architecture`, `exceptions`, `policy`.
Exit 0 means passed, 1 means violations, 2 means invalid inputs or incomplete
tool execution. Reports bind base/head/policy SHAs, installed tool versions,
findings, typing coverage and broad-handler inventory. Checks do not rewrite
source, policy or baselines, import runtime code, or connect to Azure.

### Policy and execution evidence

The four governance records have closed schemas: unknown/missing fields,
duplicate keys/IDs/sites, invalid dates/versions, unsafe paths, inconsistent
scope references and duplicate move destinations fail. Module records include
explicit typing status, public/private ownership and exact adapter forwarding
declarations. An inventoried facade may forward through a declared adapter
chain only if it terminates at an existing, declared canonical export. Extra
imports, mutable state, initialization calls or changed forwarding order fail.

Protected typing identities survive import/path moves; new modules enter
blocking coverage independently of candidate scope. Policy also requires those
additions to be recorded before merge, so coverage survives the following PR;
unknown mypy severities or malformed locations are execution errors, not
silently discarded diagnostics. An ordinary move must have
one-to-one identity and unchanged normalized syntax. Changed moves and splits
require protected responsibility/debt allocation; they are not automatically
approved by a candidate map. Individual retained debt is intersected with the
protected baseline, permitting genuine retirement without permitting revival
or duplication.

Discovery includes root namespace packages without `__init__.py`, not only
regular packages. Their concrete Python modules receive normal graph, handler
and blocking typing identities. Explicit non-runtime roots (`tests`, `scripts`,
`docs`, `infra`, `public`, generated `build`/`dist`, dependency caches and hidden
directories) remain excluded. Mypy uses explicit package bases at `src` and the
repository root so a namespace file cannot acquire conflicting flat/imported
module names. These are checker settings, not runtime `sys.path` changes.

Annotation policy compares qualified functions, overload occurrences, parameter
kinds, variadic arguments, returns, variables, decorators and local module-level
type aliases. Suppression comments are bound to syntax sites, not just counted;
moving an unchanged ignore to another error is a policy change. String literals
that merely mention `noqa` are not directives. These are static syntax checks,
not a proof about arbitrary Python metaprogramming.

Qualified and aliased `typing`/`typing_extensions.no_type_check` and
`no_type_check_decorator` uses are checked even on functions without annotated
arguments or returns. Decorator-provider bindings are part of the interface
snapshot, so changing a provider behind an unchanged `@alias` cannot silently
disable body checking.

Variable dynamic imports need exact site/context fingerprints, permitted
targets and executed behavior tests. All permitted first-party targets enter
the dependency graph. Import aliases are resolved in lexical scopes, and
package-relative imports use inventoried `__init__.py` ownership. Literal loader
aliases and keyword arguments are resolved as well. Handler fingerprints cover
the protected `try` operation, catch/outcome
and distinct occurrences, so identical handler bodies at separate sites cannot
share one allowance.

The bounded binding resolver tracks lexical assignments, builtins aliases and
plain local class-member aliases without executing application code. Handler
identity includes effective types and binding source: changing an alias from
`Exception` to `BaseException`, or rebinding a class member, invalidates the old
record. Ambiguous, cyclic, parameter-supplied or otherwise unresolved catch
expressions remain violations and cannot consume even an active record with
passing tests. Existing nominal exception imports retain their treatment,
including conditional hosted-client imports; import guards are included in
binding identity rather than forcing eager runtime imports.

`--test-evidence <file>` supplies an optional execution receipt to the quality
CLI. Without valid evidence, no exception or dynamic-import behavior approval
can be consumed. The separate `.github/scripts/run-unittest.py` uses standard
`unittest` discovery and records each exact test method, source fingerprints
and outcome. Referenced skipped/expected-failure tests are not passing evidence;
failed subtests, unexpected successes, stale source or a failed suite cannot
validate a boundary. Candidate exception records must be active (eligible for
technical validation, not independently approved),
match the exact source/caught types, have an unexpired `expires_on` date and
not have passed their `review_by_stage` (`bootstrap`, `blocking`, `strict`).
Contractual best-effort outcomes remain representable; logging is not approval.
Registry edits alone do not cause a policy-change finding. Independent human
review and approval strings are not prerequisites; `review` remains nonempty
provenance for schema compatibility. Proposed/retired records cannot be consumed.
Only technically validated exact sites exempt BLE001; other lint findings remain.

For a local, same-run receipt in PowerShell:

```powershell
$env:QUALITY_RUN_ID = [guid]::NewGuid().ToString()
python .github\scripts\run-unittest.py --base-ref <protected-base-sha> --report .artifacts\unittest.json
python .github\scripts\check-quality.py --check all --base-ref <protected-base-sha> --test-evidence .artifacts\unittest.json --report .artifacts\quality.json
```

Set `QUALITY_RUN_ID` once per run when using other shells as well. CI instead
binds the GitHub run ID and attempt. Receipts/reports bind repository, base/head,
source digest and run context; quality artifacts also bind protected policy,
toolchain and the exact receipt digest. Integrity hashes detect alteration,
not authenticity: the protected runner/evaluator and actual job conclusions
remain the trust boundary. `--pattern` on the unittest runner supports focused
local work, but the CI aggregate requires full `test_*.py` discovery.

The existing workflow extracts the protected-base runner, checker, aggregator
and tool pins when policy exists. It installs protected runtime requirements
and tool pins in a separate evaluator environment, never the candidate package,
editable metadata, dependency manifest or build backend. Ruff and mypy run in
Python isolated mode. Grimp and Import Linter analyze the explicit candidate
source directory in an isolated worker without importing application code or
adding the candidate checkout to the evaluator import path. This rejects
candidate `mypy.py`, `grimp.py`, `sitecustomize.py` and `PYTHONPATH` shadowing.
The quality matrix consumes the separate
unittest artifact, and `quality-gate` requires the real matrix, unittest and
`container-tests` results plus every fresh artifact. The container job builds
an ephemeral Ubuntu Docker image and runs `tests/container_smoke.py` with
networking disabled; it does not publish an image or deploy to Azure. Its
successful result supplies Linux image evidence for that candidate, not live
Azure integration. A missing local Docker engine is a local limitation, not
proof that Linux CI cannot run.

Tested tool pins are Ruff 0.16.5, mypy 2.3.1, Import Linter 2.14 and Grimp 3.16.
The higher Ruff/Import Linter/Grimp versions proposed during parent research
were unavailable from the configured package index; no runtime pin changed.

The initial blocking typing seeds are the canonical `config.chat_backend`,
`config.panel_config` and `config.hosted_continuity_config`. Newly introduced
modules and legacy adapters also enter blocking coverage. Existing moved
modules retain stable IDs and their prior uncovered status. The explicit
scope, future expansion and one-to-one moves are in `typing-scope.json`.
`typing-baseline.json` is empty: debt cannot be traded by total error count,
duplicated or revived after retirement. Imported legacy diagnostics stay visible
without being described as full-repository typing coverage.

Ruff selects F821, E722, BLE001, PGH003, PGH004 and RUF100. The supplementary
AST handler inventory includes logged and re-raised broad handlers that BLE001
exempts. `.quality/handler-inventory.json` records the remaining sites.
`exceptions.json` contains 30 individually documented technical boundaries:
each identifies its exact operation/catch fingerprint, rationale, diagnostic
path, observable outcome and executed failure-test selector. Records retain their
2027-01-06 expiry and blocking review stage. No blanket inherited
handler waiver is granted. Narrowing removes 35 of the original 63 broad sites;
the remaining 28 plus two companion cleanup sites preserve application-level translation, cleanup/propagation
or contractual best-effort behavior. Logging alone does not approve them.

The technical review in `.quality/exception-technical-review.json` records a
decision for every initial-adoption boundary. It is an AI technical assessment,
not independent human approval. Twenty-three diagnostic paths needed correction:
dependency exception text, exception chains and raw citation URLs must not enter
their logs. `telemetry.boundary_diagnostics.log_boundary_failure` preserves a
static operation description, exception class and terminal code location without
exception text, chained messages, source lines or local variables. The seven
unchanged boundaries retain their existing propagation/static-diagnostic behavior.
Changed handler fingerprints are now `active`, meaning eligible for technical
validation under the revised policy, not independently approved. Each must
match current source and passing bound evidence; no approval is inherited.
The new evaluator was adopted into the protected `develop` base by PR #144
(merged `ec18a73`), so the protected checker now applies it to subsequent
PRs. This assessment
does not extend expiry, reduce typing coverage, suppress checks or authorize
production deployment.

The [explicit authorization by repository administrator @placerda](https://github.com/Azure/agent-landing-zone/issues/681#issuecomment-5601804634)
accepts these exact existing records and the policy for initial administrative
adoption under Q5. It is not an independent GitHub review, a claim that bootstrap
checks pass, or authorization for production deployment. Only exact active,
unexpired records with same-source passing evidence can authorize an exception.
The initial PR (#144) reported a missing protected policy as `bootstrap-review`
because its base predated adoption; candidate data still cannot change the
evaluator's rules. This reference PR runs against the adopted `develop` base,
exercising coordinator-owned required-check controls. The recorded exception
decisions are governed by their technical evidence — exact fingerprints,
same-source bound tests and unexpired stage validation — not by a separate
independent human review step; this PR demonstrates base adoption with that
technical governance already in force.
Installed acceptance covers real auth order, upload writes/cleanup and
standalone download/OpenAPI failures outside the checkout. Linux acceptance
is supplied by the actual `container-tests` job for the PR head, not inferred
from local Windows results. The quality fixtures include disposable Git
repositories and real unittest/aggregate subprocesses for protected-policy,
receipt and false-green mutations; they do not substitute for controlled
required-check acceptance PRs. Subsequent checker/tool/workflow and other protected
policy changes still require authorized protected-policy adoption; ordinary
exception registry edits do not.
The bootstrap PR cannot approve itself by editing JSON: a base without policy
reports `bootstrap-review`. Ordinary PR checks execute the protected base's
checker and tool config as their minimum, and separately report candidate
policy changes. CI is unprivileged and `quality-gate` requires both the actual
matrix/unit-test/container results and all five fresh bound quality reports
plus the unittest receipt.

An administrator must separately require `quality-gate` and `unit-tests` on
development/release-target branches and restrict bypass. Q5 no longer mandates
independent/latest-head human approval. Existing repository review settings, if
any, are separate controls; this change does not modify them. `@placerda` was confirmed
as a repository administrator before adding CODEOWNERS. No settings were
applied. Workflow YAML, approval strings and a green local command cannot prove
required merge enforcement; bootstrap administration and controlled negative
PR evidence remain separate acceptance activities.

The decision and rollback policy are recorded in platform
`docs/adr/ADR-0020-technical-exception-validation.md`. Adopt the new protected
evaluator before claiming this policy is enforced remotely. Revert evaluator
and registry together if rolling back; do not disable checks or fabricate green
results to bridge adoption. Runtime behavior, expiry (2027-01-06), tool pins,
workflow permissions and aggregate requirements are unchanged.
Policy diagnostics describe differences from the protected files/settings,
not requests for human approval. The legacy `bootstrap-review` rule name is
retained for report compatibility; it denotes a missing protected policy, not
a requirement to solicit reviewers or obtain an administrative statement.
`CODEOWNERS` routes ownership but does not mandate latest-head approval.

### Implementation and failure-fixture map

| Scope | Implementation and executable evidence |
| --- | --- |
| T004/T008/T011/T014/T017/T020/T023/T026/T029 | Protected checker, four governance records, pinned tools and fail-closed workflow; `test_quality_policy.py` covers schemas, policy tampering, moves/splits, suppressions, dynamic bindings, debt, exact evidence, isolated execution and independent aggregation. |
| T031-T034 | Setuptools discovery, canonical config/util/auth/client/service owners, shared citations and bounded `api.history`/`services.history` split; `test_module_compatibility.py`, `test_history_boundary.py`, datalayer/citation and original security suites. |
| T035/T036 | API callbacks/routes, telemetry, bootstrap, thin legacy adapters, conditional initialization and staged roots; installed import-order/single-registration and actual startup acceptance. |
| T030/T038 | Canonical private test seams and dedicated legacy assertions; `test_installed_package.py` creates a clean non-editable environment, copies only behavioral tests and checks real auth/startup, staged resources and writable upload cleanup without runtime source leakage. |
| T037 | Docker package installation, unchanged Uvicorn startup, ephemeral image build and offline `container_smoke.py`; real aggregate dependency and image behavioral suite. |
| T041 / UI part of T045 | This contributor guide, `AGENTS.md`, PR #110 evidence and coordinated documentation/umbrella handoff. |
| Dependency failures | `test_failure_contracts.py` exercises real retry/connection parsing and concrete Azure, HTTPX, JWT and logging errors alongside unexpected-defect propagation. |
| Retained application boundaries | `test_boundary_failures.py`, the socket failure cases in `test_embed_security.py`, existing secure-download/panel-hook tests and installed startup failure acceptance; exact selectors are recorded per authorized site. |

## Recovery and coordinated documentation

This UI PR has no dependency on an unmerged peer runtime change. Restore the
previous UI artifact built from `c635bc6696714b543feec24b4a062a8a8f3ff6d0`
(runtime-equivalent to researched v2.6.2) while retaining the existing shipped
orchestrator/ingestion combination. No data, config key or RBAC migration is
needed. Revert the package/code slice through a PR if rolling source back;
do not disable required checks to bypass a checker defect. Production recovery
has not been rehearsed by this change.

Canonical contributor documentation is coordinated in
[Azure/agent-landing-zone#688](https://github.com/Azure/agent-landing-zone/pull/688). Existing local
embedding and deployment instructions retain their operator/security claims;
this change does not redesign those behaviors or publish planned controls as
already active.
