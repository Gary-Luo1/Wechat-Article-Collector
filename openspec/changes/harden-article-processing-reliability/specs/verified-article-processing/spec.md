## Purpose

Ensure that scoring, completion, and optional synchronization are based on article full text that the system actually retrieved, while exposing actionable read failures to users and automation.

## ADDED Requirements

### Requirement: Article reads have explicit outcomes
The system SHALL report an article read as successful only after non-empty full text has been retrieved and validated. Risk-control, transient network, deterministic parsing, size-limit, and HTTP failures MUST be reported as failures with distinct machine-readable classifications and recovery guidance.

#### Scenario: Full text is retrieved
- **WHEN** a queued article returns valid non-empty full text
- **THEN** the read command succeeds and records that the queued article has a verified read

#### Scenario: WeChat blocks the read
- **WHEN** WeChat returns HTTP 403, HTTP 429, or a recognized verification or risk-control page
- **THEN** the read command fails with a risk-control classification and does not report the article as read

#### Scenario: Article response cannot be parsed
- **WHEN** the response is reachable but lacks a valid article container or non-empty full text
- **THEN** the read command fails with a deterministic article-content classification rather than a transient network classification

### Requirement: Non-ad completion requires a verified read
The system MUST reject scoring, completion, and optional Feishu synchronization for a non-ad article unless the same pending article has a persisted verified read. A rejected completion MUST leave the article pending and MUST NOT create or update an external Feishu record.

#### Scenario: Completion follows a verified read
- **WHEN** a pending non-ad article has a verified read and valid five-dimension scores are submitted
- **THEN** the system completes the article using the existing scoring and synchronization policy

#### Scenario: Completion is attempted without a verified read
- **WHEN** valid scores are submitted for a pending non-ad article that has no verified read
- **THEN** the command fails, the article remains pending, and no Feishu synchronization is attempted

#### Scenario: Advertisement is explicitly skipped
- **WHEN** a pending article is explicitly completed with the advertisement flag
- **THEN** the existing advertisement skip flow remains available without requiring a verified full-text read

### Requirement: Read state is backward-compatible and bounded
The system SHALL persist only the metadata needed to prove a successful read, including the article identity, verification time, and a bounded content fingerprint. It MUST NOT persist full article text as part of this change, and existing queue files without read metadata MUST remain loadable.

#### Scenario: Existing pending record has no read metadata
- **WHEN** an existing queue is loaded after the change
- **THEN** the record remains pending and usable but is treated as not yet verified for non-ad completion

#### Scenario: Existing processed record has no read metadata
- **WHEN** a previously processed record is loaded after the change
- **THEN** it remains processed without requiring migration or reprocessing

