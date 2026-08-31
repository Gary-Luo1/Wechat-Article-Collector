## Purpose

定义通过 redfox.hk 付费 API 获取公众号文章列表与正文的行为契约：数据源配置、API 凭据管理、订阅解析、文章发现、正文使用与计费感知的调用节流，以及故障时向微信直连路径的回退。

## ADDED Requirements

### Requirement: 数据源可配置且默认不改变现有行为

系统 SHALL 提供 `settings.article_source` 配置项，取值为 `wechat` 或 `redfox`，默认值为 `wechat`。未显式配置或配置为 `wechat` 时，文章发现与正文获取 MUST 与现有微信直连行为完全一致。

#### Scenario: 默认配置走微信直连
- **WHEN** 配置中未设置 `article_source` 且已配置微信凭据
- **THEN** 发现与处理链路不发起任何对 redfox.hk 的请求，行为与变更前一致

#### Scenario: 非法取值被拒绝
- **WHEN** 配置校验遇到 `article_source` 为 `wechat`/`redfox` 之外的值
- **THEN** 校验失败并返回带该字段名的可读错误，指出合法取值

### Requirement: redfox 数据源下无需微信凭据即可发现文章

当 `article_source` 为 `redfox` 且已配置有效的 redfox API 凭据时，系统 SHALL 仅凭订阅中的公众号名称或微信号完成订阅解析与文章列表获取，MUST NOT 要求用户配置微信 cookie/token。

#### Scenario: 仅凭订阅名发现文章
- **WHEN** `article_source` 为 `redfox`、未配置微信凭据，订阅包含名称与微信号
- **THEN** 系统通过 redfox 接口解析账号并返回其文章列表，微信凭据缺失不产生错误

#### Scenario: 无匹配账号时不臆造结果
- **WHEN** 按订阅名称与微信号查询均未返回任何文章
- **THEN** 该订阅标记为 unresolved 并计入诊断，不产生队列条目

#### Scenario: 账号搜索多候选时不擅自选择（降级记录）
- **WHEN** 仅凭订阅名称（无微信号）查询
- **THEN** 系统按名称原样查询并在无结果时报告 unresolved；已决策降级：`queryWorkList` 不提供账号搜索/候选枚举端点，无法区分同名多候选，优先以订阅 `alias`（微信号）作精确键，仅有名称的订阅建议用户补充微信号（见 design.md D5 决策记录）

### Requirement: redfox API 凭据按项目安全基线管理

系统 SHALL 将 redfox API key 作为独立凭据管理：经 stdin 输入、以 0600 权限持久化、在日志/JSON 输出/错误消息中脱敏，且 MUST NOT 出现在命令行参数中。

#### Scenario: 凭据经 stdin 安全录入
- **WHEN** 用户执行凭据设置命令
- **THEN** API key 从 stdin 读取（交互终端拒绝并提示用管道），持久化文件权限为 0600

#### Scenario: 凭据不泄露到输出
- **WHEN** 任何命令成功或失败
- **THEN** 输出与日志中的 API key 一律脱敏（仅保留前若干位或完全隐藏）

### Requirement: 发现结果归一到现有队列契约

redfox 返回的文章 MUST 归一为现有队列条目结构：文章 URL 使用其 `workUrl` 原文链接并经现有 URL 归一化与去重；发布时间取 `publishTime`；重复或窗口外的文章 MUST NOT 重复入队。

#### Scenario: 新文章入队且去重
- **WHEN** redfox 返回的文章 URL 经归一化后已存在于队列
- **THEN** 该文章不重复入队，发现摘要正确计数新增/跳过

#### Scenario: 早于数据起始时间的窗口不报错
- **WHEN** 订阅的时间窗起点早于 redfox 数据起始时间 2026-04-01
- **THEN** 发现正常完成，结果仅包含 API 实际返回范围内的文章，不因数据不可得而失败

### Requirement: 优先使用 API 正文，微信直连兜底

当数据源为 redfox 且 API 响应携带正文时，处理链路 SHALL 直接使用该正文；正文缺失或为空时 MUST 回退到现有微信直连抓取路径（如可用），并在结果元数据中标注正文来源。

#### Scenario: 使用 API 正文
- **WHEN** redfox 响应的文章包含非空正文且数据源为 redfox
- **THEN** 处理结果使用该正文，不发起对 mp.weixin.qq.com 的抓取请求

#### Scenario: 正文缺失时回退
- **WHEN** redfox 返回的文章正文为空且微信直连可用
- **THEN** 系统回退到直连抓取，结果元数据标注正文来源为直连

### Requirement: redfox 错误使用现有错误协议分类

对 redfox API 的调用失败（认证失败、限流、网络错误、畸形响应）SHALL 映射到现有结构化错误协议（错误码、是否可重试、下一步动作），MUST NOT 以未分类异常中断命令。

#### Scenario: 认证失败可诊断
- **WHEN** API key 无效或过期
- **THEN** 返回结构化错误，指引用户重新设置 redfox 凭据，且不重试

#### Scenario: 限流为可重试错误
- **WHEN** redfox 返回限流或服务端临时错误
- **THEN** 错误标记为可重试并按退避策略重试，重试次数用尽后返回结构化失败

### Requirement: 计费感知的调用节流

数据源为 redfox 时，发现周期 SHALL 最小化付费调用次数：同一订阅在配置的检查间隔内 MUST NOT 重复发起列表查询；单周期内对无新增的订阅不追加分页请求（取够时间窗即停）。

#### Scenario: 时间窗内不重复查询
- **WHEN** 同一订阅距上次成功发现不足配置的检查间隔
- **THEN** 本次周期跳过该订阅的 redfox 查询，摘要标记为跳过

#### Scenario: 取够即停分页
- **WHEN** 分页拉取的文章发布时间已早于时间窗起点
- **THEN** 停止翻页，不请求后续页

### Requirement: doctor 覆盖 redfox 数据源健康检查

`doctor` 命令 SHALL 在 `article_source` 为 `redfox` 时检查 redfox 凭据存在性与 API 连通性，并将结果计入对应健康项；凭据缺失 MUST NOT 被误报为微信凭据故障。

#### Scenario: redfox 凭据缺失的体检结论
- **WHEN** `article_source` 为 `redfox` 且未配置 API key
- **THEN** doctor 报告 redfox 凭据缺失及修复指引，微信凭据项不因此失败
