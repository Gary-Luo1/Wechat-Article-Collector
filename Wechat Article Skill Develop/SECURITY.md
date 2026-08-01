# Security policy

## Reporting

Report vulnerabilities through a private GitHub security advisory. Do not open a public issue containing credentials, Base identifiers, private article content, or reproduction data copied from a real account.

## Credential model

- WeChat Cookie and token are account-session secrets.
- Agent dialogue is the primary setup path, but the Agent must warn that ordinary chat may be retained, offer only secret-input controls that the current platform actually provides, obtain consent, collect one value at a time, and never echo it.
- Not echoing a credential is response redaction, not encryption. It avoids a second copy in Agent output but does not remove, encrypt, or prevent retention of the original user message.
- Credentials are passed to the bounded writer through process stdin or a restricted consume-and-delete inbox. Users can instead choose the hidden local terminal fallback.
- Configuration and queue state are stored outside the installed Skill.
- POSIX configuration files are written with user-only permissions; Windows relies on profile ACLs.
- Feishu app secrets must use `lark-cli config init --app-secret-stdin`; they must never be pasted into ordinary chat or process arguments.
- A selected existing lark-cli App credential may instead be copied into the
  Skill-owned isolated profile after a redacted preview. The source config is
  read-only and fingerprinted, keychain identifiers are never displayed, and
  user authorization/token entries are never imported.

If credentials may have been exposed, revoke the browser session, sign in again, and rerun local setup.

## Threat model

The project explicitly defends against:

- Prompt injection in article content.
- SSRF and unsafe redirects from article URLs.
- Unbounded response or context growth.
- Queue corruption and concurrent lost updates.
- Wrong-article writes caused by shifting queue indices.
- Duplicate Feishu records and optimistic sync state.
- Cross-Agent Feishu app/profile confusion, bot impersonation fallback, blind field creation, and retries of non-transient permission errors.
- Secrets in logs or repository files.

Discovery relies on private WeChat browser endpoints. Endpoint changes and platform enforcement are availability risks, not security guarantees.
