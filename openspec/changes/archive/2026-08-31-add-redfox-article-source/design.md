## Context

现状：文章获取分三段——订阅解析与列表发现走 `wechat_api.py`（用户微信 MP cookie+token 直连 mp.weixin.qq.com），正文抓取走 `article_reader.py`（直连文章页 HTML 解析），入队/评分/飞书同步与数据源无关。错误体系为结构化协议（code/retryable/next_action，`protocol.py`），HTTP 会话与风控探测在 `http_client.py`，凭据均经 stdin 录入、0600 落盘、输出脱敏。约束见 specs/redfox-article-source/spec.md。

redfox.hk 侧事实：POST `https://redfox.hk/story/api/gzhData/queryWorkList`，鉴权头 `REDFOX_API_KEY`，按 `account`（微信号）/`accountName`（名称）分页查文章列表（每页 20），返回 `title`、`workUrl`、`publishTime`、`content`（正文）等；另有账号信息/关键词搜索接口可用于订阅解析。数据起始时间 2026-04-01。

## Goals / Non-Goals

**Goals:**
- 数据源可切换：`settings.article_source = wechat | redfox`，默认 `wechat`，零配置零行为变化。
- redfox 模式下整条链路（解析→发现→取正文）不依赖微信凭据。
- 复用既有基建：错误协议、HTTP 会话、URL 归一化去重、队列锁与原子写、doctor 体系。
- 计费感知：节流以"最小化付费调用"为第一目标。

**Non-Goals:**
- 不移除或降级微信直连路径（长期保留为默认与兜底）。
- 不引入 redfox 的关键词搜索、互动数据等本变更用不到的接口。
- 不做 API key 的加密存储改造（沿用现有明文 0600 基线）。
- 不迁移 2026-04-01 之前的历史文章。

## Decisions

### D1: 新增独立 `redfox_client.py`，不侵入 `wechat_api.py`
redfox 的请求形态（POST+JSON+API key 头）与微信 MP 接口（GET+cookie+token）差异大，塞进同一文件会破坏现有错误分类的清晰度。新客户端复用 `http_client.new_session` 与 `protocol` 分类，抛出 `RedfoxAPIError(code, retryable, next_action)`，由调用方按现有协议包装。
*备选*：改造 `wechat_api.py` 为多后端抽象——被否，抽象层收益低且触碰风控敏感代码。

### D2: 数据源分派点放在 `discover_only.py` 的发现入口与 `process_pending` 的正文获取处
订阅解析→列表→入队的编排已在 `discover_only.discover_articles`，只在其入口按 `article_source` 分派到 redfox 或现有路径；正文获取同理在 `process_pending` 侧选择"队列元数据里 API 正文 → `article_reader` 兜底"。队列条目新增可选 `metadata.content_source`（`redfox` | `direct`）与发现阶段缓存的 `content`（纯文本，可选字段，现有消费者忽略未知字段即可兼容）。
*备选*：发现与抓取统一走 `wechat_api` 的接口形状（列表不含正文、正文必二次抓取）——被否，浪费已付费拿到的正文。

### D3: API key 存储复用凭据文件模式，命令面收敛到 `manage.py`
新增 `manage redfox-set-key`（stdin 读取，交互终端拒绝，`init_config` 的 `_agent_stdin_setup` 范式：`isatty` 检查 + 64KB 限长）与 `manage redfox-status`。配置存 `credentials` 节新槽位 `redfox_api_key`，沿用 `secure_write_json`。
*备选*：环境变量 `REDFORX_API_KEY`——被否，与环境变量泄漏面（进程列表/诊断导出）冲突，仅作文档提示不作文档化支持。

### D4: 节流复用执行策略框架，语义改为"每订阅冷却时间"
`execution_policy` 增加每订阅的 `last_discovered_at` 记录（配置文件已有订阅级健康/时间信息可挂靠）：冷却期内跳过该订阅的 API 调用；分页以"发布时间早于窗口起点即停"截断。不做全局 QPS 限制（redfox 限流表现为 4xx，由错误分类+退避兜底）。

### D5: 订阅解析优先用 `account`（微信号）精确查询，`accountName` 仅作回退
微信号是稳定标识（对应现有订阅 alias），`accountName` 是搜索语义易多候选。**决策记录（评审后降级）**：`queryWorkList` 不提供账号搜索/候选枚举端点，无法实现"多候选列出供选择"；因此 redfox 路径以 `alias` 为精确键，仅名称订阅无结果时报 unresolved，并在文档中建议补充微信号；`RedfoxAccountAmbiguous` 保留用于未来接入账号搜索端点。多候选时沿用 `resolve_subscriptions` 的"列出候选、不擅自选择"行为仅适用于微信路径。biz 不可得时允许订阅的 `biz` 字段为空——URL 归一化与去重不依赖 biz。

### D6: 正文来源元数据随队列透传
发现阶段将 redfox `content` 清洗为纯文本后随队列条目缓存（可选字段）；`process_pending` 有缓存正文则免抓取，无则走 `article_reader`。飞书同步的 `content_source` 标注写入 Bitable 便于审计，字段缺失时按 `direct` 处理（向后兼容旧队列）。

## Risks / Trade-offs

- [redfox 可用性/计费政策变化] → 数据源可配置 + 微信直连完整保留，切换回 `wechat` 即回滚；doctor 增加 redfox 连通性检查提前暴露故障。
- [正文格式未知（纯文本 or HTML）] → 客户端内做启发式清洗（含标签则剥标签），实现期用一个真实 key 实测一例再定稿清洗函数（见 tasks 第 1 项）。
- [API key 泄露面] → 沿用 stdin+0600+脱敏基线；`redfox-status` 输出仅显示尾 4 位。
- [队列条目缓存正文增大 queue.json 体积] → 正文超长（>100KB）时不缓存，处理时回退直连抓取。
- [数据起始时间 2026-04-01] → 规格 Scenario 已约定不报错；文档明示。
- [并发发现重复扣费] → 发现阶段本就在 `process_lock` 内执行，锁语义不变即可。

## Migration Plan

1. 全部改动为增量（默认 `wechat` 不生效），无配置迁移。
2. 部署顺序：先合入客户端与配置项（纯新增），再合入分派逻辑；任一步出问题 revert 即回滚。
3. 用户侧启用路径：`manage redfox-set-key` → 配置 `article_source: redfox` → `manage doctor` 验证。

## Open Questions

- ~~redfox `content` 字段的实际格式（纯文本/HTML）~~ 已部分解决：错误响应形态实测确认（JSON 信封 `{"code":3106,"msg":"缺少API Key…"}`，鉴权头为 `X-API-Key` 而非 `REDFOX_API_KEY`）；正文格式实现了启发式清洗（含标签则剥标签），待有真实 key 后用一例正文验证清洗效果。
- 优质库与广域库两个接口变体的选择——先用文档指定的 `queryWorkList`（优质库），若正文缺失率不可接受再评估广域库，属实现期微调。
