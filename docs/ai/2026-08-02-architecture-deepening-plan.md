# 架构深化实施计划（2026-08-02）

> 来源：2026-08-02 架构审查报告（`architecture-review-20260802-2035.html`）中的 5 个候选，全部纳入本计划。
> 状态：计划待确认后执行。未经确认不自动 commit、push 或分派子 Agent。
> 风险等级：标准风险（本地工具代码，无生产数据、无对外发布、无数据迁移）。

## 0. 基线（已实测）

| 环境 | 结果 |
|---|---|
| 干净 venv（requests + beautifulsoup4 + pytest） | 188 passed, 3 skipped |
| 当前机器环境（缺 bs4） | 15 failed, 173 passed, 3 skipped |

15 个失败全部集中在需要解析器的路径：9 个 `article_reader` 测试 + 6 个 ingest 测试。复核 `cmd_ingest` 确认它调用 `fetch_article` 真实抓取文章页并解析发布者，因此 ingest 需要 bs4 属预期设计，不是缺陷。队列隔离设计本身有效：队列专用命令（list / inbox / dismiss / restore / digest-plan）在阻塞 `article_reader` 导入的测试中仍通过。

结论：当前机器只缺开发依赖 bs4。动手前先建立可复现基线（Step 0），后续每一步的绿色/红色判定都以该基线为准，不把环境缺失混入回归结果。

## 1. 变更地图

| 候选 | 模块 / 文件 | 职责 | 本次改动 |
|---|---|---|---|
| 1 | scripts/url_identity.py（新增） | 文章 URL 身份：校验、规范化、去重键、http 升级 | 新建 stdlib-only 模块，收敛 4 份规则 |
| 1 | scripts/article_reader.py | 抓取与解析文章页 | 不再拥有 allowlist / canonicalize 规则，改为引用 |
| 1 | scripts/queue_helpers.py | 并发安全本地队列 | normalize_url 改为引用或薄包装 |
| 1 | scripts/wechat_api.py、scripts/bitable_client.py | 发现 API 格式化、飞书记录构造 | URL 升级逻辑改为引用 |
| 1 | scripts/process_pending.py | ingest / read / done / sync | 删除惰性 URL 包装（不再需要） |
| 2 | scripts/article_inbox.py、scripts/queue_helpers.py | 收件箱查询 / 队列存储 | 新增汇总与已知 URL 查询 interface |
| 2 | scripts/manage.py（_doctor）、scripts/discover_only.py | 诊断报告 / 发现去重 | 停止读原始 pending / processed 结构 |
| 3 | scripts/process_lock.py（新增） | 跨平台文件锁 | 新建，合并两份锁实现 |
| 3 | scripts/queue_helpers.py、scripts/config_store.py | 队列事务 / 配置事务 | queue_lock、config_lock 改为调用 |
| 4 | scripts/runtime.py | 命令运行时 | 删除 _data_dir 副本，消费 paths |
| 5 | scripts/feishu_target.py、process_pending.py、manage.py、bitable_client.py | 飞书目标适配 | 见 Step 5（设计门禁） |
| 全部 | tests/ | 回归与新增行为 | 每步配套测试 |
| 全部 | docs/ai/、CHANGELOG.md | 计划与变更记录 | 本计划文件；收尾时追加 CHANGELOG |

## 2. 明确不修改的范围

- 微信抓取端点、五维评分、飞书字段 schema、lark-cli 协议调用。
- 安装器架构（install.sh / install.ps1）、打包工具（tools/）、CI（.github/workflows/）。
- 队列与配置 JSON 的结构和版本（无迁移，无数据改动）。
- .agents/、.claude/、.github/skills/ 适配器副本（只读）。
- 不新增任何第三方依赖（本计划全部步骤只使用标准库）。

## 3. 可复用模式（已在代码中验证）

- 惰性导入 + 阻塞 __import__ 的测试手法（tests/test_core.py::test_queue_only_process_command_does_not_require_article_parser）——用于验证「模块不拉入解析器依赖」。
- 跨进程并发测试（tests/test_config_store.py::test_config_lock_serializes_cross_process_updates 的双进程 bump 模式）——用于锁模块回归。
- paths.secure_write_json() 原子写、WECHAT_ARTICLE_HOME 测试重定向——全程无需真实用户数据。

## 4. 实施步骤

### Step 0 — 建立可复现基线

- **结果**：本机可稳定复现「188 passed, 3 skipped」。
- **范围**：仅本地环境，无代码改动。
- **做法**：按 README 开发流程安装依赖到隔离 venv（或经用户确认后安装到当前 Python）；记录 Python 与依赖版本。
- **验收标准**：`python3 -m pytest -q` 全绿；`python3 -m compileall -q skills/wechat-article-subscriber/scripts tests tools` 通过。
- **失败回退**：无代码改动；只影响本地环境，可随时卸载。
- **执行记录（2026-08-02 实测）**：
  - 隔离 venv：`/tmp/was-venv-baseline`（Python 3.9）。
  - 安装：`/tmp/was-venv-baseline/bin/pip install -r skills/wechat-article-subscriber/requirements.txt -r requirements-dev.txt`
  - 全量测试：`/tmp/was-venv-baseline/bin/python -m pytest -q -p no:cacheprovider`
  - 结果：基线 188 passed, 3 skipped → 全部 ticket 完成后 217 passed, 3 skipped；`compileall` 与 `tools/validate_release.py` 均通过。

### Step 1 — URL 身份模块（候选 1）

- **结果**：文章 URL 身份规则只有一份实现，且纯 URL 校验不再拖入 bs4。
- **范围**：新增 scripts/url_identity.py；article_reader.py、queue_helpers.py、wechat_api.py、bitable_client.py、process_pending.py 改为引用；新增 tests/test_url_identity.py；允许列表测试改为从 url_identity 导入。
- **前置**：Step 0。
- **接口草案**（只消除歧义，不复制实现）：
  - `is_wechat_article_url(url) -> bool`：原 article_reader.is_wechat_article（含路径解码与转义校验）。
  - `canonicalize_wechat_article_url(url) -> str`：原 article_reader.canonicalize_wechat_article_url（http 升级 + allowlist，非法抛 ValueError）。
  - `normalize_article_url(url) -> str`：原 queue_helpers.normalize_url（去重键：只保留 __biz/mid/sn/idx）。
  - `upgrade_wechat_article_url(url) -> str`：合并 wechat_api.format_article 与 bitable_client._https_wechat_url 的仅升级行为（失败原样返回）。
  - 新模块顶层不得 import requests / bs4。
- **验收标准**：
  1. 阻塞 article_reader 导入时，url_identity 全部函数可用（新增测试，沿用阻塞 __import__ 手法）。
  2. 现有行为逐字节不变：队列去重键、allowlist 校验、format_article 输出、飞书记录 URL 均与重构前一致（现有测试回归 + 新模块直接测试）。
  3. 无 bs4 环境下，5 个纯 URL 允许列表测试通过；ingest 与解析测试仍因真实抓取需要 bs4（预期保留，不作为失败项）。
  4. grep 确认 4 个模块中不再存在重复实现。
- **验证命令**：`python3 -m pytest -q`；`python3 -m compileall -q skills/wechat-article-subscriber/scripts tests tools`；缺 bs4 环境定向跑 `pytest tests/test_core.py -k allowlist`。
- **失败回退**：删除 url_identity.py 并还原 5 个引用文件（单提交内可整体还原）。

### Step 2 — 队列汇总 interface（候选 2）

- **结果**：manage._doctor 与 discover_only 不再直接读 pending / processed 原始结构；汇总与「已知 URL」查询收敛到一个模块。
- **范围**：article_inbox.py（或 queue_helpers.py）新增 queue_summary() 与 known_urls()；manage.py 的 _doctor、discover_only.py 改调用；新增/调整测试。
- **前置**：Step 1 完成（避免 URL 与队列身份规则交叉改动）。
- **接口草案**：
  - `queue_summary() -> dict`：字段与 query_inbox() 现有 summary 一致（pending / processed / favorites / later / dismissed / sync_pending）。
  - `known_urls() -> set[str]`：pending 的 normalized_url 与 processed 键的并集，供发现去重。
- **关键约束**：先逐字段比对 _doctor 与 query_inbox 的汇总语义（favorites 含 pending+processed、later 仅 pending、dismissed 仅 processed、sync_pending 仅 processed）。若历史语义不一致，先记录差异并让用户确认统一口径，禁止静默改变 doctor 输出。
- **验收标准**：
  1. _doctor 输出 JSON 与重构前逐字段一致（快照式回归断言）。
  2. queue_summary() / known_urls() 有直接单元测试（空队列、favorite/later/dismissed/sync_pending 组合、去重集合）。
  3. grep -rn 'read_queue()' 调用点收敛到队列模块内部与极少数真正需要原始数据的写路径。
- **验证命令**：`python3 -m pytest -q`；定向跑 tests/test_management.py -k doctor 与 tests/test_core.py -k "inbox or queue"。
- **失败回退**：还原 manage.py / discover_only.py 调用点。

### Step 3 — 跨平台文件锁模块（候选 3，ticket 05）

- **结果**：一份 fcntl/msvcrt 锁实现，两个存储模块共用。
- **范围**：新增 scripts/process_lock.py；queue_helpers.queue_lock 与 config_store.config_lock 改为调用；新增锁模块直接测试。
- **前置**：无（可与 Step 4 并行）。
- **接口草案**：`process_lock(path: Path, timeout: float = 10.0) -> Iterator[None]`（超时抛 TimeoutError，消息含锁路径）。
- **验收标准**：
  1. 跨进程并发回归通过（双进程 bump 测试，沿用 test_config_lock_serializes_cross_process_updates 模式）。
  2. 队列并发回归通过（test_core.py 队列测试）。
  3. 新增超时路径测试：持锁后第二次获取在 timeout 内抛 TimeoutError（Windows 按现有 skip 约定处理）。
- **验证命令**：`python3 -m pytest -q`；定向跑 tests/test_config_store.py 与 tests/test_core.py -k "lock or queue"。
- **失败回退**：还原两个调用点，删除新模块。

### Step 4 — 状态目录单一来源（候选 4，ticket 03）

- **结果**：runtime.py 消费 paths.data_dir() / paths.venv_dir()，删除 _data_dir 副本。
- **范围**：runtime.py；新增空 XDG_STATE_HOME 边界测试。
- **前置**：无（可与 Step 3 并行）。
- **验收标准**：
  1. runtime._venv_python 行为不变（test_runtime_venv_follows_state_override 通过）。
  2. XDG_STATE_HOME 为空字符串时 runtime 与 paths 均回退到 ~/.local/state（新增测试，覆盖当前实际分歧）。
  3. grep -n '_data_dir' 确认副本已删除。
- **验证命令**：`python3 -m pytest -q`；定向跑 tests/test_core.py -k runtime。
- **失败回退**：还原 runtime.py。

### Step 5 — 深化 Feishu target（候选 5，设计门禁，ticket 06）

- **结果（目标形态）**：manage 与 process 的飞书操作穿过同一条窄 seam；测试使用内存 adapter。
- **范围**：feishu_target.py、process_pending.build_feishu_target、manage.py 的 _feishu_* 命令、bitable_client.py、lark_runtime.py；配套测试。
- **前置**：Step 1–4 完成；本步开工前需用户确认方案（见「未决项」）。
- **方案 A（推荐，增量）**：把 target 工厂收敛进 feishu_target 模块；manage.py 的飞书命令改为与 process 共用同一 FeishuTarget 构建与 check/sync 语义；不改 bitable_client 内部结构。风险低，先拿到「一条 seam」的收益。
- **方案 B（彻底）**：把身份解析、档案、建库、字段映射、upsert 全部收进深模块，bitable_client 成为内部实现细节。改动大、回归面宽，需单独细化后再并入本计划。
- **验收标准（以方案 A 为例）**：
  1. manage 与 process 共用唯一 target 构建函数（grep 可验证单点）。
  2. 新增内存 adapter 直接测试 check() / sync() 行为（沿用现有 fake lark CLI 测试模式，不在测试里绕过真实调用链）。
  3. 188 回归全绿；doctor 输出不变。
- **验证命令**：`python3 -m pytest -q`；定向跑 tests/test_management.py -k feishu、tests/test_feishu_mapping.py、tests/test_feishu_patch.py。
- **失败回退**：还原调用点与工厂位置。

## 5. 全局验证路径

- 每步最小门禁：`python3 -m pytest -q` + `python3 -m compileall -q skills/wechat-article-subscriber/scripts tests tools`。
- 收尾门禁：`python3 tools/validate_release.py`；可选 `python3 tools/package_release.py --output dist`。
- 回归基线：Step 0 之后的 188 passed / 3 skipped；环境缺失的失败一律标注「环境缺失（缺 bs4）」，不混入功能回归。
- 无 UI 变更，不需要截图类证据；行为证据以测试输出与 doctor JSON 对比为准。

## 6. 风险与回退汇总

| 风险 | 影响 | 缓解 |
|---|---|---|
| Step 1 去重键语义漂移 | 队列重复入队或漏去重 | 现有去重测试 + 新模块直接测试锁行为 |
| Step 2 汇总口径变化 | doctor/status 数字变化 | 先比对口径、快照回归，禁止静默改变 |
| Step 3 锁回归 | 并发写损坏 config/queue | 双进程并发测试 + 超时测试 |
| Step 4 路径漂移 | venv 与 config 分家 | 边界测试 + 现有 override 测试 |
| Step 5 回归面大 | 飞书流程损坏 | 方案 A 增量落地 + 定向 feishu 测试全量回归 |

所有步骤无数据迁移、无 schema 变更；回退方式均为按提交/文件还原，不涉及删除用户数据。未经授权不 commit、不 push、不创建 PR。

## 7. 未决项（需要用户确认）

1. **Step 5 方案选择**：先做方案 A（增量，推荐），还是直接方案 B（彻底重构）？
2. **本机环境**：允许按 README 向当前 Python 安装 bs4（beautifulsoup4），还是统一在隔离 venv 里跑测试？
3. **Step 5 若命名新的领域概念**：项目无 CONTEXT.md，按架构审查约定应在深化时创建并记录词汇；确认是否允许。
