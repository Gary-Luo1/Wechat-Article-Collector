# Operations and recovery

Use the management command as the first diagnostic boundary. It emits a stable JSON envelope and never returns Cookie, token, Base token, or table ID.

For discovery, `WECHAT_ACCESS_RESTRICTED` means an authenticated endpoint rejected
the request (for example HTTP 403). `WECHAT_TOKEN_EXPIRED`,
`WECHAT_COOKIE_EXPIRED`, `WECHAT_CREDENTIAL_CONTEXT_INVALID`, and
`WECHAT_RATE_LIMITED` use the same safe JSON detail contract: operation name plus
HTTP status, numeric API return code, or response type. Details never contain a
Cookie, token, URL, or response body. Access restriction is not retried
automatically and does not imply that a Cookie has expired.

```text
bash scripts/run.sh manage doctor
bash scripts/run.sh manage doctor --online
bash scripts/run.sh manage status
bash scripts/run.sh manage config-show
```

`doctor` reports runtime/dependency availability, redacted configuration, queue counts, lark-cli compatibility, health history, `setup_stage`, and `next_action`. Offline mode performs no network calls. Online mode validates WeChat, resolves subscription candidates, and runs the read-only Feishu preflight when enabled. Add `--save-resolved` only after exact results have been shown or confirmed.

`status` is the compact user-facing view. It returns a percentage, completed/current/
pending/optional steps, a localized next-action label, queue counts, warnings, and
the config path without credential values.

## Persisted execution policy

Use one policy confirmation to replace repeated routine prompts:

```text
manage execution-policy show
manage execution-policy set --mode autopilot --unlisted-publisher <ask|ingest_once|auto_subscribe> --feishu-provisioning <allow|deny> --feishu-sync <allow|deny> [--base-name <BASE> --table-name <TABLE>]
# after the user approves the complete preview
manage execution-policy set <SAME_ARGUMENTS> --yes
```

Provisioning `allow` requires exact Base and table names. A confirmed autopilot
policy covers routine discovery/read/score/queue/export, its selected unlisted
publisher behavior, exact-name standard Base provisioning, and qualified writes to
the unchanged configured target. Provisioning approval is consumed after one
successful creation so retries cannot silently create duplicates. It never covers
OAuth completion, new scopes,
App/identity/manager/target/schema changes, forced below-threshold writes, deletion,
or reset. Configure Feishu identity/App/manager/target choices first and the policy
last; those changes invalidate an earlier confirmation.

## Partial dialogue configuration

The Agent may patch one section without asking the user to repeat unrelated values:

```text
setup --agent-stdin --section wechat
setup --agent-stdin --section subscriptions
setup --agent-stdin --section settings
setup --agent-stdin --section preferences
setup --agent-stdin --section feishu
setup --agent-stdin --section execution_policy
```

Accepted settings include `check_hours`, `request_delay`, `max_articles_per_account`, `content_dedup`, `min_score`, and `output_language` (`auto`, `zh`, or `en`). Ask for preferences in dialogue; do not require users to edit JSON.

Subscription maintenance is local and explicit:

```text
manage subscriptions list
manage subscriptions add --name <EXACT_NAME>
manage subscriptions bulk-add --name <NAME> --name <NAME> --dry-run
manage subscriptions bulk-add --file <NAMES.txt-or-JSON> --dry-run
manage subscriptions remove <NAME_OR_ALIAS_OR_BIZ>
discover --resolve-subscriptions --format json
discover --resolve-subscriptions --save-resolved --format json
```

Ambiguous search results require user choice. A missing result is not silently removed.
`bulk-add` accepts repeated names, newline-delimited UTF-8 text, or a JSON array of
names/objects. It validates the whole batch before one atomic save, skips existing
identities, caps a batch at 100, and supports a non-mutating dry run.

## Article inbox

Use the structured inbox rather than parsing the legacy text list when filtering:

```text
process --format json inbox --status pending --sort newest
process --format json inbox --status all --query AI --limit 20
process --format json inbox --status processed --account <EXACT_ACCOUNT>
process --format json inbox --favorite --state later
process --format json inbox --status processed --disposition dismissed
```

The inbox searches titles, publishers, digests, processed summaries, and tags. It
returns pending indices only for interactive convenience while retaining URLs as
the stable selector. Summary counts include pending, processed, favorites, later,
dismissed, and sync-pending articles; `matched` is the full filter count and
`returned` reflects the limit.

Inbox organization uses reversible local state and stable URLs:

```text
process --format json inbox-mark --link <URL> --favorite
process --format json inbox-mark --link <URL> --unfavorite
process --format json inbox-mark --link <URL> --later
process --format json inbox-mark --link <URL> --active
process --format json dismiss --link <URL>
process --format json restore --link <URL>
```

Favorite and later-reading markers do not complete an article. Dismiss moves a
pending item to processed with disposition `dismissed`; restore moves that same
article back to pending. None of these commands writes Feishu.

## Preferences and digest planning

Preferences can be updated without re-entering credentials:

```text
manage preferences show
manage preferences set --include-topic AI --include-topic engineering
manage preferences set --exclude-keyword promotion --preferred-account <EXACT_ACCOUNT>
manage preferences set --digest-hours 48 --digest-limit 10
manage preferences clear --yes
```

Repeated values replace the corresponding list; omitted fields are preserved.
`clear` previews unless `--yes` is supplied. Included topics increase candidate
priority, preferred accounts increase priority, and excluded keywords filter
matching title/publisher/digest metadata.

Generate a bounded candidate list before the Agent reads any article:

```text
process --format json digest-plan
process --format json digest-plan --hours 24 --limit 5
process --format json digest-plan --include-later
```

The result includes selection reasons and exclusion counts. It explicitly reports
that content was not fetched, articles were not completed, and Feishu was not
written. The Agent must still read untrusted article content, apply all five score
dimensions, and request any required external-write authorization.

## Direct-link ingestion

```text
process --format json ingest --url <WECHAT_URL>
```

The command safely extracts the article title, publisher, publication time, digest,
and bounded body. It does not place the body in the queue. A confirmed autopilot
policy applies `auto_subscribe` or `ingest_once` automatically. If the saved rule is
`ask` (or no policy is confirmed), it returns
`SUBSCRIPTION_CONFIRMATION_REQUIRED` before changing either config or queue. Ask the
user, then run exactly one:

```text
process --format json ingest --url <WECHAT_URL> --subscribe
process --format json ingest --url <WECHAT_URL> --no-subscribe
```

When page metadata has no publisher, collect the name from the user and add
`--account <NAME>`. `--subscribe` is current-command authorization to modify the
local subscription list; never use it speculatively. Automatic subscription must
come from the persisted policy. `--no-subscribe` queues the article once. Repeated
ingestion is URL-idempotent.

After ingestion, use the existing `read` and `done` workflow. For an explicit user request to write this individual article regardless of its score, combine `--feishu --force-feishu`. `--force-feishu` is invalid without `--feishu` and does not change the saved score threshold.

## Safe disable and reset

All destructive reset commands preview targets unless `--yes` is supplied:

```text
manage feishu-disable
manage feishu-disable --yes
manage reset --scope credentials
manage reset --scope credentials --yes
manage reset --scope queue
manage reset --scope queue --yes
manage reset --scope all-data
manage reset --scope all-data --yes
```

Credential reset preserves subscriptions, settings, and queue but clears the
execution policy together with credentials and Feishu bindings. Queue reset removes
only the queue and its lock. All-data reset removes the local config, queue, lock,
versioned config backups, unconsumed one-time Agent configuration inboxes, generated
Feishu authorization QR images, known queue/config recovery artifacts, and the
Skill's isolated lark-cli home/work directories. It uses an explicit application
artifact allowlist: unknown files and directories under `WECHAT_ARTICLE_HOME` are
preserved, including when that variable points at a portable directory. It does
not delete the installed Skill, isolated Python (`venv`) or lark-cli package
(`lark-cli`) runtimes, global lark-cli configuration, WeChat data, or Feishu Base
data.
Deleted all-data state is not recoverable by the Skill.

## Stable command protocol

Machine-readable commands return one JSON object:

```json
{"ok":true,"data":{},"next_action":"none"}
```

Failures use `error.code`, a redacted `message`, `retryable`, and `next_action`. Agents should branch on the code, not parse human prose. Article reads distinguish `ARTICLE_RISK_CONTROL`, `ARTICLE_TRANSIENT`, `ARTICLE_HTTP_ERROR`, `ARTICLE_CONTENT_INVALID`, `ARTICLE_RESPONSE_TOO_LARGE`, and `ARTICLE_READ_REQUIRED`; only `ARTICLE_TRANSIENT` is retryable. A failed discovery response can include safe `meta` counts for preserved partial progress. `process` accepts global formatting before the subcommand: `process --format json list`.

Configuration format changes are versioned. The first migration preserves a restricted `config.vN.backup.json`; `manage reset --scope all-data --yes` removes these backups too.
