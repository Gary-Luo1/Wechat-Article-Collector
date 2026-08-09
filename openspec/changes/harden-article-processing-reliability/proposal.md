## Why

The article-processing workflow can currently treat an unavailable full text as a successful read, then allow scoring and completion, while batch fetching can bypass the configured request delay and increase WeChat risk-control exposure. Discovery and retry behavior also conflate recoverable and deterministic failures, which can discard useful partial progress and give automation the wrong recovery action.

## What Changes

- Make article fetch outcomes explicit and machine-readable, including successful full-text reads, risk-control blocks, transient network failures, and deterministic parse or validation failures.
- Require a verified successful full-text read before a non-ad article can be scored, completed, or synchronized; advertisements keep their existing explicit skip path.
- Apply configured request pacing to direct article reads, reuse a bounded HTTP session within a batch, and stop the batch immediately when WeChat risk control is detected.
- Retry only errors that can reasonably recover, preserving actionable error classifications through CLI JSON envelopes.
- Preserve successfully discovered articles incrementally when a later subscription fails, while continuing to stop globally for expired credentials, rate limits, and risk-control conditions.
- Add regression, state-integrity, partial-failure, and compatibility tests for these behaviors.
- Keep the work focused on reliability; do not perform broad file splitting, unrelated refactoring, dependency upgrades, or changes to scoring and Feishu authorization rules.

## Capabilities

### New Capabilities

- `verified-article-processing`: Full-text read state, structured fetch outcomes, and the invariant that non-ad completion requires a verified read.
- `risk-aware-article-fetching`: Shared pacing and session reuse for direct article reads, typed retry decisions, and batch termination on risk control.
- `resilient-subscription-discovery`: Incremental preservation of successful account results and explicit handling of account-local versus global discovery failures.

### Modified Capabilities

None. This repository does not yet contain main OpenSpec capability specifications.

## Impact

- Affected code: `article_reader.py`, `http_client.py`, `process_pending.py`, `protocol.py`, `queue_helpers.py`, `discover_only.py`, and their focused tests.
- Persisted queue records gain backward-compatible read-state metadata; existing pending and processed records must remain readable without a destructive migration.
- CLI text and JSON error behavior becomes more precise. Existing success payloads and command selectors remain compatible unless a command previously reported false success for an unavailable article.
- No new external service, paid dependency, permission, Feishu schema change, scoring-rule change, or real-account write is introduced.
- Release evidence must include the full local test suite and release validation, while explicitly treating real WeChat access and risk-control behavior as a separate manual verification boundary.

## Execution Gate

- Status: `executable`; Gray approved implementation on 2026-08-08.
- Risk: high, because the change affects the core read-score-complete invariant, persisted queue state, external HTTP behavior, and the boundary before Feishu writes.
- Allowed implementation scope: the affected scripts and focused tests listed above, plus directly related Skill/reference documentation and release-validation fixtures when required by the changed contract.
- Prohibited scope: unrelated modules, broad formatting or module splitting, dependency upgrades, credential files, real production data mutation, automatic Feishu writes, commits, pushes, and releases.
- Pause conditions: any need to change scoring, advertisement classification, Feishu authorization, queue identity, public CLI selectors, default delay, or add a dependency; any destructive migration; or any conflict with existing user changes.
- Rollback: revert the implementation diff while preserving queued articles; additive read metadata remains harmless to the previous reader and requires no deletion.
- Implementation owner: Codex. Independent review owner: Codex review phase after implementation self-check; author self-check MUST NOT be reported as independent review.
