## 1. API 实测与客户端基础

- [x] 1.1 已完成：错误形态 {code:3106}、成功码 2000、鉴权头 X-API-Key；正文经 queryWork 实测为纯文本（无 HTML），广域库数据新鲜（T+1）。用真实 API key 实测 `queryWorkList`：确认返回字段、`content` 格式（纯文本 or HTML）、分页行为与错误响应形态，把结论回填 design.md 的 Open Questions
- [x] 1.2 新建 `scripts/redfox_client.py`：会话复用 `http_client.new_session`，POST+`REDFOX_API_KEY` 头，定义 `RedfoxAPIError(code, retryable, next_action)` 并映射认证失败/限流/网络错误/畸形响应到现有错误协议
- [x] 1.3 实现账号查询（`account` 精确优先、`accountName` 回退、多候选列出不自动选择）与分页文章列表拉取（发布时间早于窗口起点即停止翻页）
- [x] 1.4 实现正文清洗：含标签则剥标签为纯文本，超长（>100KB）不缓存返回 None

## 2. 配置与凭据

- [x] 2.1 `config_store.py`：新增 `settings.article_source`（枚举 `wechat|redfox`，默认 `wechat`，非法值校验报错）；`credentials` 节新增 `redfox_api_key` 槽位
- [x] 2.2 `init_config.py`：agent payload 与交互式初始化支持新配置项与凭据录入（stdin、isatty 拒绝、64KB 限长，复用 `_agent_stdin_setup` 范式）
- [x] 2.3 `manage.py`：新增 `redfox-set-key`（stdin）与 `redfox-status`（仅显示尾 4 位）子命令；`--token-stdin` 式冗余参数不引入

## 3. 数据源分派与发现

- [x] 3.1 `discover_only.py`：发现入口按 `article_source` 分派；redfox 路径完成订阅解析→列表→归一入队（`workUrl` 走现有 `url_identity` 归一化去重，`publishTime` 为发布时间，`biz` 允许为空）
- [x] 3.2 队列条目新增可选 `metadata.content`（缓存正文）与 `metadata.content_source`，旧条目缺失字段按 `direct` 兼容
- [x] 3.3 订阅级发现冷却时间（`execution_policy`）：冷却期内跳过 redfox 查询，发现摘要标记跳过计数

## 4. 正文获取与处理

- [x] 4.1 `process_pending.py`：数据源为 redfox 且队列缓存正文非空时直接使用，不请求 mp.weixin.qq.com；缓存缺失回退 `article_reader`，元数据标注来源
- [x] 4.2 `bitable_client.py`：同步时透传 `content_source` 标注（字段可选，缺失按 direct）

## 5. 健康检查与文档

- [x] 5.1 `manage.py doctor`：`article_source=redfox` 时检查凭据存在性与 API 连通性，微信凭据项不因此失败；拆分 try 块避免订阅解析错误误标微信健康
- [x] 5.2 更新 SKILL.md、references/operations.md（启用路径与数据起始时间 2026-04-01 限制）、references/security.md（新凭据槽位）、CHANGELOG

## 6. 测试与验收

- [x] 6.1 redfox 客户端单测：认证失败/限流重试/畸形响应的错误分类，多候选行为，分页截断
- [x] 6.2 分派与配置测试：默认 `wechat` 行为不变，非法值校验，redfox 模式免微信凭据
- [x] 6.3 发现/入队测试：去重、时间窗、缓存正文透传与回退
- [ ] 6.4 全量测试通过（使用项目 venv 的 Python 运行），并手工跑一次 `manage doctor` + 真实 key 的发现冒烟
