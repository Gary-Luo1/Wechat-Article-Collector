## Why

当前项目获取公众号文章依赖用户从微信 MP 平台提取的 cookie+token 直连 mp.weixin.qq.com：凭据频繁过期、请求受风控（环境检查/验证码/限流）影响，`WECHAT_RISK_CONTROL`、`WECHAT_AUTH_EXPIRED` 一类失败是日常运行的主要摩擦来源。redfox.hk 提供按公众号查询作品列表并直接返回正文的付费 API，可以在不持有微信凭据的前提下完成"发现 + 取正文"，作为可配置数据源接入能显著降低运维负担。

## What Changes

- 新增 redfox.hk 数据源客户端（`redfox_client.py`）：调用 `/story/api/gzhData/queryWorkList` 等接口，按订阅的 account/accountName 分页拉取文章列表与正文。
- 新增数据源配置项 `settings.article_source`（`wechat`（默认）| `redfox`），发现与处理链路按配置选择数据源，微信直连保留为兜底/回退。
- 新增 `REDFOX_API_KEY` 凭据槽位：沿用项目安全基线（stdin 输入、0600 文件存储、日志与输出脱敏、doctor 健康检查）。
- `process_pending` 在数据源为 redfox 且 API 已返回正文时直接使用，`article_reader` 直连抓取作为兜底路径。
- 发现周期节流策略适配按次计费语义（避免空转烧钱；无新文章时最小化调用次数）。
- 文档更新：SKILL.md、references/operations.md、security.md、CHANGELOG。

## Capabilities

### New Capabilities
- `redfox-article-source`: 通过 redfox.hk 付费 API 作为文章数据源的配置、凭据管理、订阅解析、文章列表发现、正文获取与计费感知节流的行为要求。

### Modified Capabilities
<!-- 现有 specs 目录尚未建立主规格；订阅发现与文章处理行为以 redfox 数据源为增量在本变更的新能力规格中完整描述，不修改既有能力要求。 -->

## Impact

- 新增：`skills/wechat-article-subscriber/scripts/redfox_client.py`。
- 修改：`discover_only.py`（数据源分派）、`process_pending.py`（正文来源选择）、`config_store.py` / `init_config.py`（配置项与凭据槽位）、`manage.py`（凭据设置命令、doctor 检查）、`execution_policy.py`（节流策略）。
- 依赖：`http_client.py` 会话与错误分类体系复用，无新第三方依赖（POST/JSON 用 requests 即可）。
- 外部依赖：redfox.hk API 可用性、`REDFOX_API_KEY` 计费账户；数据起始时间 2026-04-01（更早历史文章不可得）。
- 测试：新增 redfox 客户端单测、数据源分派与节流测试；现有测试保持通过。
