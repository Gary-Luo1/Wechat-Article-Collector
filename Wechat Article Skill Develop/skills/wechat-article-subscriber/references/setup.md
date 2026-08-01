# Installation and WeChat dialogue setup

## Supported environments

- Python 3.9+
- Network access to `mp.weixin.qq.com`
- A local Agent that supports Skills plus shell, filesystem, and network tools
- Optional Feishu: Node.js 18+ and a compatible `@larksuite/cli`

Sandboxed Agents without those capabilities can discover the Skill but cannot execute it. After installation, restart/open the Agent and say “配置微信公众号文章订阅”; configuration is dialogue-first, not a shell wizard.

## Repository installer

```text
bash install.sh --target agents
.\install.ps1 -Target agents
```

Targets: `agents`, `codex`, `claude`, `copilot`, or `all`. For another Agent, install the canonical Skill to an exact folder:

```text
bash install.sh --target agents --destination /custom/skills/wechat-article-subscriber
.\install.ps1 -Target agents -InstallPath C:\custom\skills\wechat-article-subscriber
```

Do not combine a custom destination with `all`. The installer atomically backs up an existing copy, installs only canonical files, and creates an isolated runtime in application state. `WECHAT_SKILL_INSTALL_ROOT` remains available for CI. `--no-deps` / `-NoDeps` requires `requests` and `beautifulsoup4` in the selected runtime.

## Front-loaded configuration

Collect configuration in one opening dialogue before routine work starts. Run
`setup --guide --format json` and use its `configuration_manifest`. Determine:

- credential input channel, subscriptions, search window, and unlisted-publisher behavior;
- whether Feishu is skipped, mapped, or provisioned (a required explicit choice,
  never a default inferred from omission);
- when Feishu is used: identity, exact App ID, human manager, target or exact Base/table names;
- whether standard provisioning and qualified-record sync may run automatically.

Show one bounded summary after these choices. Preview it with `manage
execution-policy set ...`, ask once, then persist the same command with `--yes`.
Configure the policy last because a later identity, App, manager, target, or schema
change invalidates it. After confirmation, continue all covered validation,
provisioning, discovery, reading, scoring, queueing, export, and sync work without
asking again.

Pause only for an OAuth/device page the user must complete, unresolved ambiguity,
expired credentials, a new scope or changed target/schema, a forced below-threshold
write, or a destructive action.

## Configuration choice and secret transport

Run `setup --guide --format json` and show the user the returned absolute
`local_config_file.path`, `required_fields`, and `minimal_template`. State clearly
that the file is plaintext JSON protected by the current OS account permissions;
it is not encrypted and must not be committed, synced, uploaded, or shared.

Let the user choose one route before collecting any values:

1. Send Cookie and token in ordinary chat, one at a time, after acknowledging the
   platform retention risk. The Agent passes the assembled payload over stdin and
   never echoes the values.
2. Edit the returned local configuration path directly using the minimal template,
   save it, and tell the Agent to continue. The Agent can first run
   `setup --prepare-local-file --format json` to create the parent directory and a
   valid empty skeleton without overwriting an existing file. With the user's
   self-edit choice, `setup --open-local-file --format json` opens it in the OS
   default editor without returning its contents. After editing, the Agent
   runs `setup --validate-local-file --format json`, which reports only redacted
   readiness and never echoes values.
3. Run local `setup` and enter the secrets at hidden terminal prompts.

Do not present an unavailable encryption or secret-input feature as a working
option. A masked input control may be used only when the current platform actually
provides it, and masking still must not be described as storage encryption.
Likewise, “the Agent will not echo the value” describes response redaction only.
It does not encrypt, delete, or prevent retention of the original chat message.

## Secret transport capability matrix

| Agent capability | Method | Rule |
|---|---|---|
| Ordinary chat after consent + process stdin | `setup --agent-stdin` | Convenient, but chat may be retained |
| User edits returned local config path | `config.json` | No credential enters chat; plaintext local storage |
| Filesystem API but no stdin | `setup --prepare-agent-file`, write exact inbox, then `setup --agent-file` | Inbox is restricted and consumed once |
| Neither safe channel | Local hidden-input `setup` | User enters secrets in terminal |

Never put Cookie/token in command arguments, environment variables, repository files, arbitrary temporary files, logs, or responses. Before ordinary chat input, explain retention risk and obtain consent. Ask one field at a time and never quote it back.

The local-file lifecycle is:

```text
setup --prepare-local-file --format json
setup --open-local-file --format json
# user edits the returned path
setup --validate-local-file --format json
manage status
```

Preparation never overwrites an existing file. If an existing file is invalid,
repair it explicitly rather than replacing it. Validation reports missing field
names, subscription count, search-window readiness, and value-free Cookie/token
shape diagnostics only.

## WeChat setup

Start by running `setup --guide --format json`. Show the exact local file path and
filling requirements, then ask the user to choose chat input, direct file editing,
or the local hidden-input wizard. Do not collect a credential before this choice.

Do not send users to a deep `cgi-bin/home` link; it may not open before a valid
session exists. Instead:

1. Open `https://mp.weixin.qq.com/` and sign in.
2. Open browser developer tools (`F12`) → Application.
3. Open Storage → Cookies → `https://mp.weixin.qq.com/`.
4. Copy every cookie row and join the values as `name=value; name=value`; do not copy only selected keys.
5. Copy the numeric `token` query parameter from the current authenticated page URL.

Never accept a token from `/wxamp/`. The Cookie commonly contains the diagnostic
keys `rand_info` and `slave_bizuin`; session keys can include `slave_sid`,
`slave_user`, `bizuin`/`data_bizuin`, and `data_ticket`. These names are a
checklist, not permission to copy only those fields. Display names only, never
Cookie values.

Then ask for exact subscribed account names and a required search window:
“每次希望搜索多久以内的文章？24 小时（推荐）、48 小时、7 天，还是自定义？”
If the user skips it, explicitly say that 24 hours will be used; never apply the
default silently. Values above 48 hours with a per-account result limit of 10 or
less may miss articles on busy accounts, so warn and offer to raise the limit.

Also ask the required Feishu destination question before policy confirmation:
skip Feishu, map an existing Base, or create a new Base. Persist the answer with
`manage feishu-destination --mode skip|existing|create`. The default
`undecided` state blocks routine execution; it must never be converted to `skip`
because the user did not answer.

Build the full payload in memory and send it over the selected safe channel:

```json
{
  "wechat_cookie": "<secret>",
  "wechat_token": "<secret>",
  "subscriptions": ["Exact Account", {"name":"Another","alias":"optional"}],
  "settings": {"check_hours":24,"output_language":"auto"},
  "feishu": {"destination": "skip", "enabled": false},
  "execution_policy": {
    "confirmed": true,
    "mode": "autopilot",
    "unlisted_publisher": "ask",
    "allow_feishu_provisioning": false,
    "provision_base_name": "",
    "provision_table_name": "",
    "allow_feishu_sync": false,
    "approved_at": "",
    "scope_version": 1
  }
}
```

Include `confirmed:true` only after the user approved the displayed policy. When
configuration was saved before approval, use the management command instead:

```text
manage execution-policy set --mode autopilot --unlisted-publisher ask --feishu-provisioning deny --feishu-sync deny
# show the preview once, then persist the identical choice
manage execution-policy set --mode autopilot --unlisted-publisher ask --feishu-provisioning deny --feishu-sync deny --yes
```

Then validate and resolve:

```text
discover --check-token --format json
discover --resolve-subscriptions --format json
```

Show candidates for ambiguous accounts and save only explicit/exact choices with `--save-resolved`. Credentials can be very short-lived; validate immediately after capture. Distinguish expired session (refresh both values) from wrong context/incomplete Cookie (copy the full cookie set again from Application storage and refresh the token from the current authenticated page URL).

Use partial setup sections later so the user does not re-enter unrelated values. See [operations.md](operations.md). For optional Base creation/mapping and user authorization, read [feishu.md](feishu.md). For credential safety and article prompt-injection boundaries, read [security.md](security.md).

## Local state

- Windows: `%APPDATA%\wechat-article-subscriber`
- macOS: `~/Library/Application Support/wechat-article-subscriber`
- Linux: `$XDG_STATE_HOME/wechat-article-subscriber` or `~/.local/state/wechat-article-subscriber`
- Override: `WECHAT_ARTICLE_HOME`

Run `manage doctor` for resumable setup state and `manage reset` for explicit previewed cleanup.
Use `manage status` for the compact user-facing progress view; it reports completed,
current, optional, and pending steps plus a localized next-action label.
