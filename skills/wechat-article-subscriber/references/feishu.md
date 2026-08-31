# Feishu dialogue setup

Feishu is optional. During the opening configuration dialogue, determine: create a
new Base, map an existing table, or skip; `user` or `bot`; exact App ID; manager;
target or Base/table names; and whether provisioning/sync are allowed. Include these
choices in the single execution-policy confirmation. Never default, fall back, or
switch silently.

Persist the destination choice immediately. `undecided` blocks execution and is
never equivalent to `skip`:

```text
manage feishu-destination --mode skip
manage feishu-destination --mode existing
manage feishu-destination --mode create
```

Persist a manually selected identity with:

```text
manage feishu-identity --as user
manage feishu-identity --as bot
```

All later Base commands must use the confirmed identity with explicit `--as user`
or `--as bot`.

## Guided onboarding (feishu-setup)

Two target modes are confirmed with the user before anything is written:
- **Existing table** — the user pastes the table URL; `manage feishu-target --url <link>`
  parses the base/table tokens, verifies read access and the real fields (read-only),
  and saves the mapping. Nothing is created.
- **New standard table** — the wizard shows the exact 11-field list and only creates
  after the user confirms names and fields (one execution-policy approval).

Bot identity runs the whole chain without an OAuth scan: bind app → pipe App Secret →
`feishu-create-base --yes` (created as the bot) → the configured human manager is
granted full access automatically. Personal identity instead uses one minimal-scope
device authorization (Base read/write + offline_access only).

`manage feishu-setup` is the single entry point for Feishu configuration. It never
assumes prepared app information: for the current state it returns the question to
ask the user, the exact next command, and — when no app exists yet — console
guidance for creating one (open.feishu.cn, enterprise custom app, Base
read/write scopes). The App Secret enters only through
`printf %s '<APP_SECRET>' | manage feishu-app-secret`, which pipes it into the
isolated profile and immediately probes that it works.

## Search scope

Discovery of existing configuration is limited to the Skill's isolated runtime and the user's own global lark-cli profile list (`manage feishu-local-profile scan`). When nothing is found there, never broaden the search to other accounts, machines, or guessed credentials — stop and ask the user whether to install lark-cli, supply App credentials, or skip Feishu.

## Importing an existing local lark-cli profile (macOS keychain limitation)

`manage feishu-local-profile scan` lists every global lark-cli profile and requires an
explicit App ID selection, so multiple local configurations are always distinguished —
never guessed. After `import --yes`, the Skill immediately probes whether the isolated
profile can actually decrypt the App Secret. On macOS, lark-cli stores App Secrets in
the system keychain bound to the global configuration directory, so the isolated clone
usually cannot decrypt them (`app_secret_resolvable: false`). The remediation is a
one-time copy of the App Secret from the Feishu Open Platform console
(open.feishu.cn) piped through stdin:

```text
printf %s '<APP_SECRET>' | bash scripts/run.sh lark config init --app-id <APP_ID> --app-secret-stdin
```

Device-authorization errors mentioning a missing `client_secret` are classified as a
configuration gap with this same guidance; never run `config init --new`, which the
Skill blocks because it can replace unrelated app configuration.

## Current Feishu bot conversation

When the setup conversation itself is arriving through a supported Feishu/Lark
bot, the trusted host/event context is authoritative for the conversational bot:
use the exact configured App ID and the current event's sender Open ID instead of
asking the user to type them. After the destination choice, pass only these
non-secret identifiers through stdin:

```json
{"source":"lark-channel","app_id":"cli_exact","sender_open_id":"ou_exact"}
```

```text
manage feishu-host-context --agent-stdin
```

The command selects `bot`, saves an Agent binding for the exact App ID, records
the invoking human as manager, never echoes the sender Open ID, and invalidates
any older execution policy whose App/identity/manager scope changed. Then bind
the isolated CLI to that detected source and verify it. Verification reads the
isolated `profile list`, selects exactly one profile whose `appId` equals the
current conversation App ID, saves that profile name locally, and injects it into
all later commands. An unrelated active/default profile is ignored and is never
used as a fallback. Zero matches or duplicate App-ID matches stop the flow. If the
host cannot supply both exact identifiers, or if either conflicts with saved
configuration, stop and use the manual flow below; never infer from display names.
Event `sender_id` is an Open ID (`ou_...`), while the host/connector configuration
supplies the App ID (`cli_...`).

## CLI and identity

In this reference, `manage ...` and `lark ...` are Skill wrapper subcommands. Run
them as `bash scripts/run.sh manage|lark ...` or
`.\scripts\run.ps1 manage|lark ...`. Never invoke `lark-cli` directly for this
Skill.

Run `manage doctor` or `lark --version`. The tested release is `@larksuite/cli@1.0.69`; compatible releases are `>=1.0.69,<2`. If missing, verify Node.js 18+ and npm, explain the package and destination, and obtain permission before installing it locally:

```text
npm install --prefix <APP_STATE>/lark-cli @larksuite/cli@1.0.69
```

On Windows, `npm install` can fail with `EBUSY: resource busy or locked` when
Defender or another process is scanning the freshly written package. Retry the
same command after a short pause; if it keeps failing, verify nothing is
holding the target directory (`tasklist` or a second npm process), exclude the
state directory from real-time scanning, or fall back to an existing global
`lark-cli` (`>=1.0.69,<2`) and verify with `manage doctor`. The Skill accepts
either the isolated `<APP_STATE>/lark-cli` install or a compatible global
binary; the version gate is the only requirement.

The adapter resolves the native executable (`lark-cli.exe` on Windows) before
any npm `.cmd` launcher. It sets both `LARKSUITE_CLI_CONFIG_DIR` and
`HOME`/`USERPROFILE` to `<APP_STATE>/lark-cli-home`, uses
`<APP_STATE>/lark-cli-work` as cwd, removes inherited CLI app/token variables,
and fingerprints the real `~/.lark-cli/config.json` before and after each call.
This isolation is mandatory. Never point lark-cli at, switch, remove, rename,
edit, or wholesale copy the user's global profiles. The only supported reuse
path is the bounded single-App clone below.

When the required App already exists in the user-level lark-cli configuration,
the Skill may reuse it without running lark-cli against that source:

```text
manage feishu-local-profile scan
manage feishu-app --app-id <EXACT_APP_ID>
manage feishu-local-profile import
# after the user confirms the preview
manage feishu-local-profile import --yes
```

`scan` parses at most 1 MiB and returns only profile name, App ID, brand,
identity policy, secret-storage kind, and counts. It never returns a secret,
keychain identifier, token, or user Open ID. `import` requires exactly one App-ID
match and atomically clones only the selected App credential into the
deterministic Skill-owned profile. It strips every user authorization entry so a
token refresh cannot mutate shared user credentials. Inline or keychain-backed
App credentials are supported. The original config is opened read-only and
fingerprinted throughout; all later lark-cli calls still use the isolated HOME
and config directory. A `user` identity must therefore complete one isolated
Base authorization, while a `bot` identity can use the imported App credential
without a user OAuth flow.

After the identity choice and installation, choose exactly one binding mode during
the front-loaded configuration:

1. `agent`: only when a supported host Agent is detected (OpenClaw, Hermes, or
   Lark Channel). The wrapper detects the host from its environment signals
   (`OPENCLAW_HOME`/`OPENCLAW_STATE_DIR`/`OPENCLAW_GATEWAY_TOKEN`,
   `HERMES_HOME`/`HERMES_STATE_DIR`, or `LARK_CHANNEL`/`LARK_CHANNEL_HOME`/
   `LARK_CHANNEL_APP_ID`) and binds with the matching source:
   `lark config bind --source <detected source> --app-id <APP_ID> --identity user-default`.
   The detected source must equal the saved `agent_source`. Binding changes
   the Skill's isolated configuration, so include this exact choice in the
   front-loaded confirmation. If lark-cli later reports that the pinned
   `--profile` does not exist (for example lark-cli created the profile as
   `cli_<app_id>` instead of the Skill name), run
   `manage feishu-context --verify` first: it resolves the real profile by App
   ID and corrects `cli_profile` automatically instead of editing config.json
   by hand.
2. `existing`/`dedicated`: first run
`manage feishu-app --app-id <APP_ID>`. It derives a stable private profile
name from the App ID. Reuse a matching local profile through the read-only
scan/import flow above, or run
`lark config init --app-id <APP_ID> --app-secret-stdin`; the wrapper adds the
exact `--name` and rejects a mismatch. Do not pass `--profile`, use
   `profile use`, or run `config init --new`. If a new app is required, create
   it in the Feishu developer console first, then configure its explicit ID.

Only after the private profile or Agent binding is configured, run
`manage feishu-context --verify`. It returns a redacted App ID/profile/user
snapshot. Display the App ID and authorized user in the single configuration
summary. For an Agent binding, the current conversation App ID is the selection
key even when lark-cli marks another bot profile as active/default. Never select
by default status or bot display name.

Save `binding_mode`, optional `agent_source`, `expected_app_id`, and, only for
user identity, the confirmed `expected_user_open_id`. For bot identity, resolve
the invoking human's exact Open ID from the supported Lark host sender context
or `lark-contact`, then run `manage feishu-manager --open-id <OPEN_ID>`; never
infer it from a display name. Every Base preflight rejects a missing/mismatched App ID
and, when saved, a mismatched user Open ID before reading or writing the table. If
strict mode blocks user identity, explain the exact policy change and obtain
confirmation. Never request an app secret in ordinary chat; accept one only
through a safe stdin channel.

## One authorization flow and resume

First inspect the selected identity status returned by `manage feishu-context
--verify`.

Use the persistent, secret-free authorization state machine for user identity:

```text
manage feishu-auth status
manage feishu-auth start
# run lark auth login only when start returns start_single_user_base_authorization
manage feishu-auth complete
```

States are `not_started`, `waiting`, `authorized`, `expired`, `failed`, and
`not_required`. The state record contains only identity and timestamps. Device
codes and verification URLs remain in the active conversation flow and are never
written to config.

- If `user` is already ready with a valid token, reuse it and do not call `auth login`.
- If `user` is not ready, start exactly one minimum-domain flow below. Keep and
  resume that flow; never start a second flow after the user has authorized.
- If `bot` was selected, do not call `auth login` at all. Configure the app secret
  through a safe local/stdin channel, ensure the required backend scopes exist,
  and verify bot readiness.

For user identity only, request the Base domain:

```text
lark auth login --domain base --no-wait --json
```

Run that command only after `manage feishu-auth start` returns
`start_single_user_base_authorization`. If it returns
`resume_existing_user_base_authorization`, show or resume the existing active
flow; do not call `auth login --no-wait` again. If the existing flow truly
expires, preview and confirm `manage feishu-auth expire --yes` before starting a
replacement.

Forward the opaque verification URL unchanged and generate its QR image with
`lark auth qrcode` in the isolated application-state work directory. Show both, end the
turn, and wait for the user to confirm authorization; then complete the same flow
with `auth login --device-code <CODE>`, verify status once, and delete the QR image. Keep `device_code`
only in the active authorization flow—never in Skill config, repository files,
or logs. Only if the link or Agent context actually expires may the old QR/code
be discarded and a new minimum-domain flow be started.

After successful device completion, run `manage feishu-auth complete`. This
verifies the selected user identity and records `authorized`. Calling `start`
again verifies an `authorized` state before reuse, so an actually expired token
can enter one replacement flow without treating the stale state as valid.

If backend scopes are missing, show the returned console URL and request only the missing scope. A `91403` response means the authenticated user lacks Base access/role permission; fix sharing or role permissions rather than switching identity.

## Provisioning and mapping

Run `process feishu-schema` for the standard schema. Creation and schema extension
are external writes. Approve exact standard Base/table names once:

```text
manage execution-policy set --mode autopilot --feishu-provisioning allow --base-name <BASE_NAME> --table-name <TABLE_NAME> --feishu-sync <allow-or-deny>
# after the user confirms this one policy preview
manage execution-policy set --mode autopilot --feishu-provisioning allow --base-name <BASE_NAME> --table-name <TABLE_NAME> --feishu-sync <allow-or-deny> --yes
manage feishu-create-base --name <BASE_NAME> --table-name <TABLE_NAME>
```

This is the only supported standard Base creation path. Do not call
`base +base-create --fields` from a shell and do not use `@-`: the current CLI
treats `@-` as invalid field JSON. The deterministic command serializes the
bounded schema internally and invokes the native binary with a Unicode argv
array, avoiding PowerShell quoting, `.cmd` code pages, and long hand-built
commands. An exact confirmed policy match executes without another prompt. A name
mismatch previews only; `--yes` remains available for an explicitly authorized
one-off creation. Successful policy-authorized creation consumes the provisioning
approval, preventing a retry from silently creating a duplicate Base.

For bot identity, resource creation and manager assignment are one provisioning
transaction:

1. Require a confirmed `feishu.manager_open_id` before creation.
2. Create a standard Base through `manage feishu-create-base`; for other
   document types use their deterministic wrapper path.
3. Extract the returned resource token without exposing credentials.
4. The Base wrapper grants the manager internally; for other resource types run
   provide the resource token on stdin to
   `manage feishu-grant-manager --token-stdin --type <RESOURCE_TYPE>`; it is
   never accepted as a command-line value.
5. Continue to content writes only when the result contains
   `manager_granted: true` and `permission: full_access`.

For a Base, use resource type `bitable`. For documents use the exact supported
type (`doc`, `docx`, `sheet`, `slides`, `wiki`, `file`, or `folder`). The grant
uses the same verified App ID, explicit bot identity, Open ID member type, and
`full_access`. If it fails, report that the resource exists but provisioning is
incomplete; never claim completion and never silently switch identity.

For an existing Base URL:

1. Resolve it with `base +url-resolve --url <URL> --as <CONFIRMED_IDENTITY>`.
2. If no table is selected, list tables and ask the user to choose.
3. List real fields and map exact/known aliases plus compatible types.
4. Require only `title` and `url`; other mappings are optional.
5. Never treat formula, lookup, system, or attachment fields as ordinary targets.
6. Never create missing fields without a separate schema preview and confirmation.

Known aliases include `标题/文章标题`, `链接/文章链接/URL`, `公众号/公众号名称`, `摘要/文章摘要`, `发布时间/发布日期`, `评分/AI评分`, and `标签/文章标签`. Ambiguous matches require user selection.

Save only the Feishu section with `setup --feishu-agent-stdin` (or the restricted
one-time inbox), configure the execution policy last, then run `process
feishu-check --save-mapping`. The check remains read-only. A confirmed policy with
`allow_feishu_sync:true` authorizes qualified record writes to this unchanged
target; otherwise a current explicit `--feishu` request is required.
