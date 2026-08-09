## Context

See `proposal.md` for motivation. The canonical implementation is under `skills/wechat-article-subscriber/`. Article reads currently collapse several failures to `None`; `read` can therefore exit successfully without full text, while `done` validates scores but not read provenance. Direct reads create their own session and do not use the persisted request delay. Discovery accumulates all accounts in memory and only queues after the full loop returns.

The queue is local, locked, and already supports backward-compatible optional fields. Feishu synchronization intentionally saves local completion before attempting the external write. The existing Chrome impersonation and safe redirect behavior must remain intact. Real-account WeChat behavior cannot be proven by local mocks alone.

## Goals / Non-Goals

**Goals:**

- Establish a deterministic invariant between successful full-text reading and non-ad completion.
- Use one typed failure model from transport through CLI output.
- Apply conservative pacing and stop rules to direct article batches.
- Preserve successful discovery progress without weakening global safety stops.
- Keep persisted-state changes additive and rollback-safe.

**Non-Goals:**

- Persisting full article content or changing the five scoring dimensions, score threshold, advertisement heuristic, or Feishu authorization policy.
- Broadly splitting `manage.py`, `init_config.py`, or other large modules.
- Adding a new dependency, changing browser impersonation, raising default batch limits, or attempting to bypass WeChat verification.
- Proving live-account success through local tests or automatically performing real Feishu writes.

## Decisions

### 1. Replace optional fetch results with typed outcomes

Direct article fetching will return a validated article document or raise a typed failure carrying a stable code, retryability, and safe recovery action. The minimum taxonomy is risk control, transient transport, HTTP response, content parsing, and response-size failure. Protocol mapping will preserve those distinctions instead of translating them into `INVALID_ARGUMENT` or generic internal errors.

This is preferred over returning `None` plus logs because all callers need to make different state and retry decisions. A result-union object was considered, but exceptions fit the existing command boundary and failure-envelope architecture with less duplication.

### 2. Persist a read proof, not article content

After a successful read, the pending queue entry will be updated atomically with a small `read_state` object containing `status=verified`, UTC verification time, and a SHA-256 fingerprint of the bounded text. Missing state means unverified. Existing processed records are not revalidated, and existing pending records remain readable but must be read before non-ad completion.

`done --ad` remains exempt because it is the explicit disposition path and does not produce a content score. Non-ad `done` checks the read proof before calculating completion or attempting Feishu synchronization.

Persisting the full text was rejected because it expands storage, retention, privacy, and migration scope. A process-memory-only flag was rejected because separate CLI invocations are the normal workflow.

### 3. Introduce an owned versus borrowed request-session boundary

The fetch layer will accept an optional caller-owned session and pacing object. A single read creates and closes its own context. Batch read creates one context, shares it across items and retries, and closes it in a `finally` path. The pacer uses monotonic time and the persisted `request_delay`; the first request is immediate and each later outbound attempt observes the delay.

This keeps connection and fingerprint context stable within a batch without creating a global long-lived session. Global session state was rejected because command processes are short-lived and it complicates cleanup and tests.

### 4. Make retryability explicit at the transport boundary

Only connection interruption, timeout, and explicitly selected temporary server responses are retryable. HTTP 403/429 and recognized verification content become risk-control results with no retry. Invalid HTML, empty content, size-limit violations, credential failures, and ordinary non-retryable 4xx responses fail immediately. Retries remain bounded and use the existing backoff policy in addition to pacing.

Module-name checks and blanket `ValueError` retrying are removed because they cannot distinguish recoverable transport failures from deterministic domain validation.

### 5. Commit discovery at the account-result boundary

Discovery will produce one structured account result at a time. The command layer will immediately add valid articles using existing queue locking, normalization, and deduplication, then persist a newly resolved account identifier before moving to the next subscription. This avoids a new transaction format and makes reruns naturally idempotent.

Account-local conditions such as unresolved identity, missing identifiers, and invalid individual article entries are recorded and processing continues. Credential failures, risk control, rate limiting, and exhausted transient transport failures stop further requests. A failure envelope for a stopped run includes safe partial-progress metadata.

Writing after every article was considered but rejected because the existing per-account page result is already bounded and per-article locking would add unnecessary disk churn.

### 6. Preserve existing external-write ordering

The established order—validate local completion, persist it, then attempt Feishu synchronization—remains unchanged. The new read gate occurs before local completion, so a failed gate cannot create a processed record or an external write. Existing pending-sync recovery remains the rollback and retry mechanism for Feishu failures.

## Risks / Trade-offs

- [Existing pending articles become ineligible for non-ad completion until reread] → Treat missing read state as safe-unverified, document the one-time behavior, and leave advertisement disposition available.
- [Additional queue write after each successful read increases local I/O] → Store only bounded metadata and reuse the existing atomic queue lock/write path.
- [Configured pacing increases batch duration] → Preserve the user-configured delay and report progress; safety takes priority over throughput for WeChat requests.
- [A real WeChat response may not match mocked error shapes] → Add contract-like response fixtures and retain a separate opt-in manual real-account check with no claim of automated proof.
- [Incremental discovery can leave a deliberately partial run] → Make partial state explicit in text/JSON diagnostics and rely on URL-idempotent reruns.
- [Typed exceptions can affect internal callers] → Inventory all fetch callers and compatibility wrappers before changing signatures, then cover each entrypoint with regression tests.

## Migration Plan

1. Add typed failures and protocol mappings while preserving existing successful result formats.
2. Add optional queue read metadata and compatibility tests before enforcing the completion gate.
3. Route single and batch reads through the shared pacing/session boundary and enable stop-on-risk behavior.
4. Change discovery to persist per-account results and expose partial progress.
5. Run focused regression tests, the full test suite, and release validation; perform only an explicitly authorized, non-writing real-account check if credentials are available.

Rollback requires reverting the implementation changes. The additive read metadata is ignored by the current queue reader, so no destructive data rollback is required. Articles queued incrementally remain valid queue entries and must not be deleted during rollback.
