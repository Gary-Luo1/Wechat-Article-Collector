<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="WeChat Article Collector: discover, score, and optionally sync Official Account articles to Feishu — stages: discover, score, queue, Feishu">
</p>

<p align="center">
  <a href="README.md">中文</a> · <a href="README.en.md">English</a>
  &nbsp;·&nbsp;
  <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat" alt="MIT License">
</p>

> Chinese README carries the latest visual layout. This English page mirrors the same structure with shorter copy.

## What it is

An Agent Skill for local Agents that **discovers** recent WeChat Official Account articles (alias-based via paid [redfox.hk](https://redfox.hk/)), extracts **bounded** article text, applies a validated **five-dimension** score, keeps a **concurrent-safe** local queue, and **optionally** upserts to Feishu Base through an isolated `lark-cli`.

One line: **discover, score, and optionally sync OA articles to Feishu.**

---

## Proof: five-dimension scoring

<p align="center">
  <img src="./assets/readme/scoring.svg" width="100%" alt="Scoring weights: technical depth 30%, analytical depth and independent view 25%, novelty 20%, practical value 15%, quality and credibility 10%">
</p>

Each dimension is 1–10; all five keys are required. Rubric: [`scoring.md`](skills/wechat-article-subscriber/references/scoring.md). `digest-plan` only sorts/filters — it does not change scores or write Feishu.

| Dimension | Weight |
|-----------|-------:|
| 技术深度 (technical depth) | 30% |
| 分析深度与独立观点 (analysis & independent view) | 25% |
| 信息新颖度 (novelty) | 20% |
| 实用参考价值 (practical value) | 15% |
| 内容质量与可信度 (quality & credibility) | 10% |

---

## Mechanism: pipeline

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Pipeline: dialogue setup → redfox alias discovery → read and five-dimension score → local inbox; skip Feishu or sync Feishu Base via isolated lark-cli">
</p>

Canonical code lives in `skills/wechat-article-subscriber/`. Adapters under `.agents` / `.claude` / `.github` only make the Skill discoverable.

---

## Quick start

```bash
# macOS / Linux (portable default: ~/.agents/skills)
bash install.sh --target agents

# Windows PowerShell
.\install.ps1 -Target agents
```

1. Create an API key at [redfox.hk](https://redfox.hk/) (pay-per-call)
2. Restart/open the Agent and say: **配置微信公众号文章订阅**
3. Fill in Key, subscriptions (account name + WeChat alias), time window, Feishu destination (skip / map / create)
4. Confirm the execution policy once — then discover → read → score → queue → (optional) sync

> Pass secrets via stdin / local hidden input / controlled inbox — **never** CLI args, repo files, or logs. Local `config.json` is plaintext; do not commit it.

Targets: `agents` · `codex` · `claude` · `copilot` · `openclaw` · `hermes` · `all`  
Skill files only: `--no-deps` (requires `requests` and `curl_cffi` on the system Python).

---

## Key commands

From the install directory (on Windows use `.\scripts\run.ps1`):

```bash
bash scripts/run.sh discover --hours 24
bash scripts/run.sh manage status
bash scripts/run.sh manage doctor
bash scripts/run.sh process --format json inbox --status pending --sort newest
bash scripts/run.sh process read --link "https://mp.weixin.qq.com/s/..."
bash scripts/run.sh process sync-feishu --all --dry-run
```

Prefer `--dims-file scores.json` for scoring. Feishu/setup details: [`feishu.md`](skills/wechat-article-subscriber/references/feishu.md), [`setup.md`](skills/wechat-article-subscriber/references/setup.md).

---

## Layout

```text
skills/wechat-article-subscriber/   # canonical Skill
.agents/skills/  .claude/skills/  .github/skills/
tests/  tools/
install.sh / install.ps1
```

---

## Stack

| Layer | Notes |
|-------|--------|
| Runtime | Python 3.9+ |
| HTTP | `requests`, `curl_cffi` |
| Source | redfox.hk wide library (alias query, paid) |
| Optional Feishu | Node.js 18+, `@larksuite/cli` (isolated config) |
| Spec | [Agent Skills specification](https://agentskills.io/specification) |

---

## Limits

- Discovery depends on WeChat-side private web endpoints that may change or rate-limit; respect platform terms and local law.
- redfox is paid; Feishu sync needs authenticated `lark-cli` and is optional.
- Cloud sandboxes without outbound network or package install cannot run discovery directly.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## License

MIT. See [LICENSE](LICENSE).
