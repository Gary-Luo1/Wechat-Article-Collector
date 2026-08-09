# Changelog

## 2.3.0 - Unreleased

### Changed

- Unify article URL identity rules (validation, canonicalization, dedup key,
  and http-to-https upgrade) in one stdlib-only module shared by queue,
  discovery, ingestion, parsing, and Feishu records; pure URL validation no
  longer requires the HTML parser dependency.
- Impersonate a real Chrome TLS/header fingerprint for the private discovery
  API and article reads via `curl_cffi` when installed, with a plain
  `requests` fallback for existing runtimes that do not reinstall.
- Add browser-like `Referer`, `Accept-Language`, and `X-Requested-With`
  headers, and route `discover --check-token` through the configured request
  delay instead of firing immediately.
- Route doctor/status queue statistics and the known-URL dedup query through
  one queue-module interface instead of raw storage reads.
- Share one cross-platform process lock between the queue and configuration
  stores.
- Resolve the state directory and venv location from a single paths module in
  the command runtime.
- Move the Feishu target wiring to one production construction point shared by
  processing and management flows.

### Fixed

- Doctor online checks now report an incompatible lark-cli version as a Feishu
  validation failure instead of bypassing the compatibility check.
- Article reads stop immediately on WeChat risk-control verification pages
  (环境异常 / verification markers) and on HTTP 403/429 instead of retrying
  and compounding the block; the discovery API treats HTTP 429 as an immediate
  rate-limit stop as well.

## 2.2.0 - Unreleased

### Added

- Restore and generalize multi-Agent platform adaptation: OpenClaw, Hermes, and
  Lark Channel are detected from their environment signals and bound with the
  matching `lark config bind --source`; installers gain `openclaw` and `hermes`
  targets (`~/.openclaw/skills`, `~/.hermes/skills`).
- Normalize pasted WeChat Cookies from DevTools table layouts (semicolon,
  newline, tab, or `name: value` rows) into a canonical header, and flag masked
  or redacted tokens (e.g. `***`) with targeted guidance instead of a generic
  shape error.
- Add a Windows-safe one-time file channel for the trusted Feishu host context
  (`manage feishu-host-context --agent-file`) alongside the existing
  `--agent-file` setup inbox, and document the no-pipe PowerShell flow.
- Self-heal `cli_profile` drift: `manage feishu-context --verify` resolves the
  real lark-cli profile by App ID for both Agent and manual bindings instead of
  requiring a config.json edit.
- Create Feishu Base tables from a bounded `@base-fields.json` file in the CLI
  work directory instead of inline JSON, avoiding Windows quoting failures.

### Fixed

- Re-running the full `setup --agent-stdin` flow no longer resets configuration
  that the host does not resend: Feishu binding, confirmed execution policy,
  settings, and preferences are merged section-by-section instead of rebuilt
  from defaults, matching the partial-patch semantics.
- Partial `execution_policy`, `settings`, and `preferences` updates keep every
  omitted field (confirmed, sync approval, unlisted-publisher behavior, etc.)
  instead of resetting it to defaults.
- Installer `--target all` now installs to every supported platform, including
  the new OpenClaw and Hermes targets.
- Concurrent Base creation no longer collides on a shared fixed-named fields
  file; each call uses a process-unique temporary file.

## 2.1.0 - Unreleased

### Added

- Add read-only discovery and previewed import of an exact existing local
  lark-cli App profile into Skill-owned isolated state. Imports reuse inline or
  keychain-backed App credentials, strip all user tokens, and leave the original
  multi-profile configuration unchanged.
- Add an explicit Feishu destination state (`skip`, `existing`, or `create`) that
  blocks execution while undecided, plus trusted stdin import of the current
  Feishu bot App ID and sender Open ID.
- Resolve Agent-bound lark-cli profiles by the trusted current-conversation App
  ID, ignoring an unrelated active/default bot and stopping on missing or
  duplicate matches.
- Clarify in both human and machine-readable setup output that non-echoing is
  response redaction rather than encryption and does not prevent chat retention.
- Add conversational, resumable Feishu setup with optional isolated `lark-cli` installation, explicit user-or-bot identity selection, one minimum Base authorization flow for user identity, new/existing Base paths, and app-profile checks.
- Add read-only `feishu-check`, standard schema output, and field-ID mapping for existing user tables.
- Add a redacted offline/online doctor, resumable setup stages, credential health history, per-account discovery diagnostics, and stable JSON command envelopes.
- Add partial dialogue configuration, explicit subscription management/disambiguation, language/preferences, safe Feishu disable, and previewed credential/queue/all-data reset.
- Add custom installation destinations, allowlisted release archives with checksums, tagged GitHub Release automation, and automation safety/retry contracts.
- Add direct WeChat-link ingestion with safe metadata extraction, URL-idempotent queueing, explicit add/skip subscription consent, unknown-publisher recovery, and an explicit per-article Feishu threshold override.
- Add a secret-free `setup --guide` contract, explicit search-window confirmation state, and `manage feishu-context` identity diagnostics.
- Add an exact local configuration path and loadable minimal template so users can choose ordinary chat or direct self-editing without a false encryption promise.
- Add non-overwriting local config preparation, redacted validation, and compact user-facing setup progress.
- Add a persistent secret-free Feishu authorization state machine that reuses valid authorization and blocks duplicate starts while a flow is waiting.
- Add atomic bulk subscription import with dry-run/deduplication and a filterable pending/processed article inbox.
- Add reversible inbox organization with favorite, later-reading, dismiss, restore, and matching summary counts.
- Add versioned topic/account preferences plus a metadata-only digest candidate plan that never fetches, completes, scores, or syncs articles by itself.
- Add a versioned, front-loaded execution policy that records one bounded user
  confirmation for routine Agent work, unlisted-publisher handling, exact-name
  standard Base provisioning, and qualified Feishu sync.
- Add automatic policy-aware direct ingestion, Base creation, manager grant,
  verification, and score-threshold sync without repeated per-step prompts.

### Fixed

- Correctly classify lark-cli `confirmation/confirmation_required` responses as
  high-risk confirmations, and preserve fresh Feishu health after
  `feishu-check --save-mapping`.
- Preserve non-retryable permission, authorization, and confirmation
  classifications when a Feishu sync stays in the local pending outbox.
- Treat a newer WeChat or Feishu health failure as not ready even when an older
  successful-verification timestamp is still retained for audit history, and
  include Feishu validation as an explicit setup-progress step.
- Make `reset --scope all-data` remove legacy and future application-state
  artifacts such as old field caches while preserving only installed runtimes.
- Prevent lark-cli profile disruption with a forced private HOME/config directory, deterministic per-App profile, native binary resolution, inherited-credential stripping, mutation guards, and global-config fingerprint checks.
- Move standard Base schema creation behind `manage feishu-create-base`; fields are generated internally and sent to the native CLI as Unicode argv instead of shell-escaped JSON or unsupported `@-`.
- Simplify WeChat credential capture to the browser Application cookie store and current authenticated page URL; no request inspection is required.
- Grant the confirmed invoking user `full_access` immediately after every bot-created Feishu resource, with App ID verification and an explicit incomplete-provisioning failure.
- Resolve lark-cli to an absolute path and isolate its config/work directories so Skill operations cannot reorder or overwrite the user's global multi-profile CLI configuration.
- Stop automatically creating missing Base fields and stop retrying permission, authorization, mapping, and confirmation errors.
- Upgrade exact-host WeChat API article links to HTTPS before reading.
- Distinguish expired WeChat credentials from incomplete Cookie/wrong-page credential context.
- Force UTF-8 console output on Windows and accept UTF-8 BOM score/config JSON.
- Version configuration migrations and preserve a one-time restricted backup while rejecting unsupported future formats.
- Replace the non-portable deep WeChat token link and report which Cookie key names are missing without exposing values.
- Stop guessing the conversational Feishu bot: require a confirmed App ID, distinguish supported Agent binding from existing/dedicated profiles, and optionally pin the authorized user Open ID before table access.
- Require identity confirmation before Feishu authorization, reuse valid user authorization, and prevent bot mode from entering the user authorization flow.
- Return the stable `link` selector in digest candidates while retaining `url` for backward compatibility.
- Include generated Feishu authorization QR images and unconsumed one-time Agent inboxes in full local-data reset.
- Invalidate automatic Feishu approval whenever the identity, App, manager, target,
  or schema boundary changes, while keeping OAuth completion, new scopes, forced
  low-score writes, and destructive actions outside autopilot.

## 2.0.0 - Unreleased

### Security

- Move credential collection to hidden local input and state outside the Skill installation.
- Add strict WeChat article URL and redirect validation, response limits, and untrusted-content delimiters.
- Redact token-bearing request details from errors and logs.

### Fixed

- Add standards-compliant Skill frontmatter and OpenAI Agent metadata.
- Remove duplicate implementations and duplicate tests.
- Repair discovery imports, entrypoint invocation, exception handling, and atomic config updates.
- Replace index-based completion/sync with stable normalized URLs and a retryable sync outbox.
- Use the current `lark-cli` field and record command contracts.
- Correct Feishu score field type and datetime representation.
- Enforce all five score dimensions and configured score thresholds.
- Wire URL normalization, content dedup settings, export, cleanup, batch-read, and sync retry commands.
- Replace global dependency installation with an isolated runtime and recoverable multi-Agent installers.
- Expand CI to compile shipped code, validate release structure, test commands, and smoke-test installers.
- Add platform launchers with `python3`, `python`, and Windows `py -3` fallback.
- Keep `WECHAT_ARTICLE_HOME` consistent between installers and runtime lookup.
- Make installation transactional across Skill targets and dependency setup.
- Quarantine structurally invalid queues and keep every `done --dry-run` path non-mutating.
- Default to URL-authoritative identity and make content-based deduplication opt-in.
- Reject encoded dot-segment and backslash escapes in WeChat article paths.
- Add project-discovery adapters for portable, Claude, and GitHub Copilot repositories.
- Add `--dims-file` examples to avoid PowerShell native-command JSON parsing differences.
- Allow local scoring and completion without a Feishu or WeChat configuration file.
- Make Agent-guided dialogue the primary configuration flow with bounded stdin ingestion, a restricted consume-and-delete inbox fallback, schema validation, redacted confirmation, and a local hidden-input fallback.
