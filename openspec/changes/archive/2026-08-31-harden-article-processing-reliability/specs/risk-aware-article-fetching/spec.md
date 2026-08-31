## Purpose

Reduce avoidable WeChat risk-control exposure by pacing direct article requests, stopping unsafe batches, and retrying only failures that can reasonably recover.

## ADDED Requirements

### Requirement: Direct article requests obey configured pacing
The system SHALL apply the persisted request-delay setting between direct article fetch attempts within a batch, including retries, without adding an unnecessary delay before the first request.

#### Scenario: Multiple articles are read in one batch
- **WHEN** batch reading issues two or more direct article requests
- **THEN** consecutive outbound attempts are separated by at least the configured request delay

#### Scenario: A single article is read
- **WHEN** only one direct article request is needed
- **THEN** the request can start immediately and the configured delay does not add a pre-request wait

### Requirement: Batch reads stop on risk control
The system MUST stop issuing further article requests as soon as any item in the batch produces a recognized WeChat risk-control result. The command MUST identify the blocked item and report how many items were successfully read before the stop.

#### Scenario: Risk control occurs in the middle of a batch
- **WHEN** an earlier article succeeds and a later article returns a risk-control result
- **THEN** the batch stops before requesting subsequent articles and reports partial progress as a failed batch outcome

#### Scenario: Risk control occurs on the first article
- **WHEN** the first article returns a risk-control result
- **THEN** no remaining article is requested

### Requirement: Retry decisions use typed failure semantics
The system SHALL retry only transient connection, timeout, or explicitly retryable server failures. It MUST NOT retry credential failures, rate limits, risk-control responses, deterministic parsing failures, invalid content, response-size violations, or non-retryable HTTP responses.

#### Scenario: Transient connection failure recovers
- **WHEN** a transient connection failure occurs and a later attempt succeeds within the configured retry limit
- **THEN** the read succeeds after bounded backoff and pacing

#### Scenario: Deterministic content failure occurs
- **WHEN** a response is too large, empty, or structurally invalid
- **THEN** the system fails after the first response without repeating the request

### Requirement: Batch network resources are bounded
The system SHALL reuse request context within one batch and release it when the batch finishes or aborts. Resource cleanup MUST occur on successful, failed, and risk-control paths.

#### Scenario: Batch aborts after an error
- **WHEN** a batch terminates because of a fetch failure
- **THEN** its request context is released and no further request is issued

