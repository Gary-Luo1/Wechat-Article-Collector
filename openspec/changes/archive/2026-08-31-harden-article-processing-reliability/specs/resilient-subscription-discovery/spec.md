## Purpose

Preserve useful discovery progress across subscription failures while retaining conservative stop behavior for global credential, rate-limit, network, and risk-control conditions.

## ADDED Requirements

### Requirement: Successful account discovery is persisted incrementally
The system SHALL add successfully discovered articles to the local queue after each subscription is processed, using existing URL identity and deduplication rules. A later subscription failure MUST NOT remove or discard articles already queued during the same run.

#### Scenario: Later subscription fails after earlier success
- **WHEN** one subscription is successfully discovered and a later subscription produces a run-blocking failure
- **THEN** articles from the successful subscription remain queued and the run reports the later failure

#### Scenario: Discovery is retried after partial progress
- **WHEN** the command is rerun after a partial failure
- **THEN** existing URL identity rules prevent duplicate pending or processed entries

### Requirement: Account-local failures do not block unrelated subscriptions
The system SHALL record an account diagnostic and continue when a failure is limited to one subscription, including unresolved identity, missing account identifier, or malformed article entries that can be safely skipped.

#### Scenario: One subscription cannot be resolved
- **WHEN** a subscription has no exact account match
- **THEN** that subscription is reported as unresolved and later subscriptions are still processed

#### Scenario: One article entry is malformed
- **WHEN** an account response contains an article entry without the required title or link
- **THEN** the invalid entry is excluded, the diagnostic records the exclusion, and other valid entries and subscriptions continue

### Requirement: Global failures stop further discovery
The system MUST stop issuing further WeChat requests after expired credentials, invalid credential context, rate limiting, recognized risk control, or exhausted transient network retries. Successful results persisted before the stop MUST remain available.

#### Scenario: Credentials expire during discovery
- **WHEN** WeChat reports an expired token or cookie while processing a subscription
- **THEN** no later subscription is requested, prior successful results remain queued, and the command returns the existing credential recovery classification

#### Scenario: Network retries are exhausted
- **WHEN** a transient network failure continues through the bounded retry policy
- **THEN** discovery stops, prior successful results remain queued, and the failure is reported as run-blocking

### Requirement: Partial discovery is observable
The system SHALL report per-account outcomes and aggregate counts for queued articles, skipped invalid entries, completed accounts, and the account that caused a run-blocking failure, without exposing credentials or token-bearing request URLs.

#### Scenario: Run stops after partial success
- **WHEN** discovery stops after at least one account completed successfully
- **THEN** text and JSON output identify the run as partial and report preserved counts without including secrets

