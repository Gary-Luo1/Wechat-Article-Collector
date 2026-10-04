# Automation contract

Automation is opt-in. Include the schedule, network activity, credential expiry
risk, and exact Feishu write boundary in the
front-loaded configuration summary. Do not create a recurring schedule merely
because setup succeeded; schedule creation remains a separate external change.

## Safe unattended boundary

The deterministic unattended action is discovery:

```text
bash scripts/run.sh discover --format json
```

It fetches configured accounts (at most `max_articles_per_account` each, default 3)
and updates the local queue. It does not read or score. Reading and scoring happen
in an Agent turn: `process batch-read` fetches every pending body, and the Agent
scores each non-ad article on the five-dimension rubric without asking the user
for scores. During that Agent turn, a confirmed autopilot policy authorizes
routine reading/scoring and qualified Feishu writes to the unchanged configured
Base/table without per-article prompts. A scheduler that only runs `discover`
does not score or write Feishu.

`process --format json digest-plan` is also deterministic and local, so it may be
used after discovery to select bounded metadata candidates. It is not a substitute
for an Agent turn: it does not fetch article bodies, score or complete articles,
or authorize/write Feishu.

Before scheduling, run:

```text
bash scripts/run.sh manage doctor --online
```

Require `runtime.supported`, installed dependencies, a valid redfox key and healthy subscriptions, resolved subscriptions, and—if enabled—successful Feishu preflight. Store no credentials in the scheduler command or environment; the command reads the restricted application-state config.

## Result handling

- Exit `0` and `ok:true`: record the queue counts; no user notification is needed unless requested.
- redfox credential errors: stop repeated attempts and ask the user to re-enter the redfox API key via stdin.
- Rate limit/transient errors: use bounded exponential backoff with jitter; do not run more frequently than the configured interval.
- After three consecutive failures recorded in health, suspend the schedule and surface the redacted `doctor` report.
- Never retry authorization, permission, wrong-app, confirmation-required, or field-mapping failures in a loop.

For recurring Feishu sync, include the exact destination and recurring-write scope
in `manage execution-policy set`, confirm it once, and use `process --format json
sync-feishu --all`. That command refuses to run until the policy allows Feishu
sync, and it still skips advertisements and scores below `min_score`. The current
explicit write, including articles scored before the policy existed, is
`sync-feishu --qualified`. A dry run remains useful for a newly mapped table but
is not a second approval gate. Pending entries remain queued until a confirmed
successful write.

To stop automation, disable/delete the scheduler entry first. `manage feishu-disable --yes` stops future Skill sync but does not modify an external scheduler or delete Base data.
