## 1. Baseline and Regression Evidence

- [x] 1.1 Confirm the approved change status is `executable`, record the protected dirty-worktree baseline, and inventory every caller of article fetch, read, completion, discovery, and protocol classification before editing.
- [x] 1.2 Add failing regression tests proving that risk-control or unavailable article text currently cannot be distinguished from a successful `read`, and that non-ad `done` can currently proceed without a verified read.
- [x] 1.3 Add failing regression tests for batch pacing, first-risk-control termination, bounded session cleanup, deterministic no-retry behavior, and transient retry recovery.
- [x] 1.4 Add failing regression tests showing that a later subscription failure currently prevents earlier successful discovery results from being queued, including an idempotent rerun assertion.

## 2. Typed Fetch and Protocol Failures

- [x] 2.1 Introduce the minimal typed article/transport failure taxonomy for risk control, transient transport, HTTP response, content parsing, and size-limit failures without adding a dependency.
- [x] 2.2 Change article fetching to return a validated document or raise the typed failure, preserving safe redirect validation, response bounds, Chrome impersonation, and secret-safe logging.
- [x] 2.3 Replace blanket `ValueError` and module-prefix retry detection with explicit retryable transport and selected temporary-server cases; verify deterministic failures issue exactly one request.
- [x] 2.4 Map each new failure to stable text and JSON CLI outcomes with correct retryability and next actions, and preserve existing credential and Feishu error codes.

## 3. Verified Read State and Completion Gate

- [x] 3.1 Add atomic queue helpers that record and read bounded `read_state` metadata containing verified status, UTC time, and a SHA-256 text fingerprint without persisting article content.
- [x] 3.2 Update single and batch read success paths to persist the read proof only after valid non-empty full text is obtained; failure paths must not create a verified state.
- [x] 3.3 Enforce the verified-read precondition before score calculation, local completion, or Feishu synchronization for non-ad articles, leaving rejected items pending.
- [x] 3.4 Preserve the explicit advertisement completion exception and verify that old pending and processed queue records remain loadable without destructive migration.
- [x] 3.5 Add state-integrity tests for read success, failed rereads, completion rejection, advertisement disposition, concurrent queue updates, and absence of Feishu calls when the gate fails.

## 4. Risk-Aware Single and Batch Reads

- [x] 4.1 Add a monotonic request pacer that uses persisted `request_delay`, starts the first request immediately, and applies the delay to each later outbound attempt including retries.
- [x] 4.2 Add an owned-versus-borrowed session boundary so a single read closes its own session while a batch reuses one session and closes it on success, ordinary failure, and risk-control abort.
- [x] 4.3 Stop batch processing on the first risk-control result, issue no later article requests, and report the blocked article plus successful partial count in text and JSON output.
- [x] 4.4 Verify existing batch limits, URL validation, response-size bounds, retry counts, and successful read output remain compatible.

## 5. Incremental and Observable Discovery

- [x] 5.1 Refactor discovery around a structured per-account result without changing subscription matching, article formatting, cutoff, pagination, URL identity, or deduplication rules.
- [x] 5.2 Queue each successful account result and persist a newly resolved account identifier before starting the next subscription, using existing atomic and idempotent storage paths.
- [x] 5.3 Continue after unresolved accounts, missing identifiers, and safely skippable malformed article entries, while recording per-account skipped counts.
- [x] 5.4 Stop further requests for expired credentials, invalid credential context, rate limits, recognized risk control, and exhausted transient failures while retaining earlier queued results.
- [x] 5.5 Add text and JSON partial-progress diagnostics that report completed accounts, queued articles, skipped entries, and the blocking account without exposing tokens, cookies, or token-bearing URLs.
- [x] 5.6 Add tests for full success, account-local continuation, partial global failure, incremental identifier persistence, idempotent rerun, and secret-safe diagnostics.

## 6. Documentation, Verification, and Handoff

- [x] 6.1 Update directly related Skill and operations/security documentation for verified-read requirements, batch pacing, partial discovery, error codes, and the one-time effect on existing pending articles.
- [x] 6.2 Run focused regression tests for article reading, queue completion, protocol envelopes, and discovery; record passed, failed, skipped, and environment-blocked results separately.
- [x] 6.3 Run `python -m pytest -q` and `python tools/validate_release.py` after the final implementation change and confirm no effective adapter or package artifact drift.
- [x] 6.4 Inspect the final diff for unrelated edits, sensitive information, accidental deletion, queue-format incompatibility, and changes outside the approved allowlist; document rollback steps and residual risks.
- [x] 6.5 Record a real-account, non-writing WeChat check as not authorized; never treat local fixtures as proof of live-account risk-control behavior and never perform an external write without explicit authorization.
- [x] 6.6 Hand off with status `待 Codex 审查`, mapping every acceptance scenario to evidence and identifying the read gate, retry taxonomy, pacing, partial discovery, and Feishu no-write boundary as independent-review priorities.
