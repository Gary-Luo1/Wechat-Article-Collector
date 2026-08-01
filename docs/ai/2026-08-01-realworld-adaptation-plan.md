# 修复计划：实战问题优化与多 Agent 平台适配

> 计划来源：2026-08-01 真实部署（OpenClaw + Windows）配置过程回顾，共 9 个问题。
> 目标：修掉实战暴露的配置/兼容性痛点，并把技能适配到更多 Agent 平台。
> 生成日期：2026-08-01

## 0. 背景与基线

实战配置在 OpenClaw + Windows 上完成（11/12 步），暴露 9 个问题。其中"配置被反复覆盖"（feishu 部分）与"重复授予"两类根因已在本会话上一批修复中解决；本计划处理剩余问题，并恢复/扩展 Agent 平台适配。

基线（已提交并推送 GitHub，main 分支）：

- 全量测试 `172 passed, 3 skipped`；`validate_release.py` exit 0；打包产物 `2.1.0`。
- 结构已平铺到仓库根目录。
- 关键现状：`agent_source` 目前只接受 `lark-channel`（OpenClaw/Hermes 已被移除），而实际部署环境是 OpenClaw；`setup` 已有 `--prepare-agent-file`/`--agent-file` 无管道路径但未在 Windows 文档中作为标准入口；`execution_policy` 部分补丁仍会重置未提及字段。

## 1. 变更地图

### 需要修改的文件

| 文件 | 职责 | 本次修改 |
|---|---|---|
| `skills/wechat-article-subscriber/scripts/init_config.py` | Agent 配置入口 | execution_policy 合并语义；Cookie/Token 容错解析；掩码识别 |
| `skills/wechat-article-subscriber/scripts/config_store.py` | 配置校验 | `agent_source` 恢复 openclaw/hermes |
| `skills/wechat-article-subscriber/scripts/manage.py` | 管理命令 | `_detect_agent_source` 表驱动恢复多平台；host-context 接受多 source；profile 自愈引导 |
| `skills/wechat-article-subscriber/scripts/lark_runtime.py` | lark-cli 运行时 | profile 缺失时的引导性错误；`safe_lark_arguments` 兼容恢复后的 source |
| `skills/wechat-article-subscriber/scripts/bitable_client.py` | 飞书操作 | Base 创建改用 `@fields` 相对路径文件；标识解析容错 |
| `skills/wechat-article-subscriber/scripts/run.ps1` | Windows 包装器 | stdin/编码稳健性（PS 5.1） |
| `install.sh` / `install.ps1` | 安装器 | 新增 `openclaw`、`hermes` 目标 |
| `skills/wechat-article-subscriber/references/setup.md`、`feishu.md`、`operations.md`、`README.md` | 文档 | Windows 无管道流程、多平台矩阵、lark-cli 安装指引 |
| `tests/` | 回归 | 各步骤对应新增测试 |
| `CHANGELOG.md` | 变更记录 | 追加本批条目（版本见决策点 D4） |

### 明确不修改的范围

- 不改微信抓取、评分、队列、飞书协议与字段 schema 核心逻辑。
- 不改变已发布的 `2.1.0` 打包产物命名逻辑（版本动作见决策点 D4）。
- 不引入新的第三方依赖。
- 不重写安装器架构，只在现有目标表上扩展。

### 现有可复用模式

- `_normalize_feishu(existing=...)` 的合并语义（本会话已实现）——execution_policy 沿用同一模式。
- `--prepare-agent-file`/`--agent-file` 一次性 inbox（init_config 已有实现）——作为 Windows 标准路径并扩展到 host-context。
- `resolve_lark_profile(expected_app_id)` 按 App ID 解析真实 profile——用于 profile 自愈。
- `_run_lark` 已固定 `cwd=lark_cli_work_dir()`——`@fields` 相对路径天然可落地。
- 表驱动平台映射（旧代码 openclaw/hermes/lark-channel 信号表）——恢复并扩展。

## 2. 实施步骤（按依赖顺序）

### Step 1：execution_policy 部分补丁合并语义（问题 7 收尾）

- **结果**：Agent 只补丁政策某字段时，未提及字段（`confirmed`、`allow_feishu_sync` 等）不再被重置为默认。
- **修改范围**：`init_config._normalize_execution_policy(partial=True)` 改为在"当前政策"基础上覆盖 payload 字段；`_apply_section_patch` 的 execution_policy 分支传入当前 `config["setup"]["execution_policy"]`。
- **前置**：无。
- **验收标准**：`setup --section execution_policy` 只发 `{"mode":"autopilot"}` 后，`confirmed`、`unlisted_publisher`、`allow_feishu_sync` 等保留；显式字段正常生效。
- **验证**：新增回归测试（构造 confirmed 政策 → 部分补丁 → 断言保留）；全量 `pytest -q`。
- **回退**：单函数回退，不影响其他步骤。

### Step 2：Cookie/Token 容错解析（问题 1/2）

- **结果**：DevTools 表格格式（多列/制表/换行/`name: value`）粘贴也能被归一化成标准 Cookie；被打码的 token 得到定向提示而非笼统报错。
- **修改范围**：
  - `init_config` 新增 `normalize_cookie(raw) -> str`：按 `;` 或换行切分，每段兼容 `name=value`、`name\tvalue`、`name: value`，去重并按原顺序拼回 `name=value; name=value`。
  - 保存 Cookie 前归一化（`config_from_agent_payload`/`config_from_feishu_payload` 相关入口）。
  - Token：写入前 `strip()`；`_warn_credential_shape`/`credential_shape` 增加掩码特征识别（`***`、`****`、`<redacted>` 等）并输出专门提示"该值疑似被会话打码，请重新发送原始数字"。
- **前置**：无（可与 Step 1 并行，但同一文件，建议顺序执行）。
- **验收标准**：表格格式 Cookie 归一化后缺失 key 提示准确；掩码 token 返回 `token_is_numeric=False` 且消息含"打码/redacted"。
- **验证**：新增单测覆盖三种行格式与掩码；全量回归。
- **回退**：单函数回退。

### Step 3：Windows 无管道配置路径（问题 3/4）

- **结果**：Windows PowerShell 下配置中文不再损坏；文档给出标准做法。
- **修改范围**：
  - 文档（`README.md`、`references/setup.md`、`references/operations.md`）：把 `setup --prepare-agent-file` → 写入 UTF-8 JSON → `setup --agent-file <path>` 列为 Windows 标准流程，并给出 PowerShell 示例；标注 `--agent-stdin` 在 PS 5.1 下编码不可靠。
  - `run.ps1`：验证/加固 stdin 透传（若 PS 5.1 仍不稳，则文档直接推荐 `& $python runtime.py setup --agent-file ...`）。
  - 可选（推荐）：为 `manage feishu-host-context` 增加同款 `--agent-file` inbox，复用 init_config 的 inbox 函数（抽取为共享工具或按当前模块边界复制最小实现）。
- **前置**：无。
- **验收标准**：按文档步骤在 PowerShell 完成含中文的 feishu 补丁，订阅中文名正确（人工验收项）；JSON 文件流程在本地全量测试中可复现。
- **验证**：本地直接跑 `--prepare-agent-file`/`--agent-file` 流程 + 全量测试；Windows 实机为人工验收项。
- **回退**：文档改动无风险；run.ps1 改动可回退。

### Step 4：profile 不匹配自愈与引导（问题 6）

- **结果**：`cli_profile` 与 lark-cli 实际 profile 名不一致时，用户不再需要手改 config.json。
- **修改范围**：
  - `manage.py`：`feishu-context --verify` 的自动同步逻辑保留并前置——在绑定/导入命令结束时主动调用一次 profile 解析与同步（复用现有 `resolve_lark_profile`）。
  - `lark_runtime.safe_lark_arguments`/相关错误路径：当注入的 `--profile` 在 lark-cli 侧不存在时，错误消息明确引导"运行 `manage feishu-context --verify` 自动纠正 profile"（若能在不递归的前提下做轻量探测则加，否则仅增强消息）。
- **前置**：Step 6（source 恢复）非必需，可先行。
- **验收标准**：把 `cli_profile` 改成不存在的名称后，`feishu-context --verify` 自动纠正为真实 profile 并返回一致结果；错误消息含引导语。
- **验证**：新增测试（mock profile list 返回真实名 → 断言 cli_profile 被纠正；错误消息断言）；全量回归。
- **回退**：单文件回退。

### Step 5：Base 创建改 `@fields` 相对路径文件（问题 8）

- **结果**：Windows cmd 下建库不再受内联 JSON 引号问题困扰，wrapper 可替代手工 lark-cli。
- **修改范围**：
  - `bitable_client.create_standard_base`：把 schema JSON 写入 `lark_cli_work_dir()/base-fields.json`（`_run_lark` 的 cwd 即该目录），传 `--fields @base-fields.json`（相对路径），调用后删除文件。
  - `created_base_identifiers`：在现有 key 匹配基础上补一层常见路径容错（如 `data.base.app_token`、嵌套层级），确认 `_find_values` 已覆盖或增强。
- **前置**：无。
- **验收标准**：`_run_lark` 收到的参数含 `@base-fields.json`（mock 断言）；字段 JSON 与现 schema 一致；dry-run 通过；本地全量测试通过。
- **验证**：单测断言参数与临时文件生命周期；`pytest -q`。
- **回退**：单函数回退。

### Step 6：多 Agent 平台适配（问题：适配性）

- **结果**：技能在 OpenClaw、Hermes、Lark Channel 及已支持的 Codex/Claude/Copilot 上都有明确的检测、安装与绑定路径。
- **修改范围**：
  - 6a `config_store.py`：`feishu.agent_source` 恢复为 `{"", "openclaw", "hermes", "lark-channel"}`。
  - 6b `manage.py`：`_detect_agent_source()` 改为表驱动：
    `openclaw: (OPENCLAW_HOME, OPENCLAW_STATE_DIR, OPENCLAW_GATEWAY_TOKEN)`、`hermes: (HERMES_HOME, HERMES_STATE_DIR)`、`lark-channel: (LARK_CHANNEL, LARK_CHANNEL_HOME, LARK_CHANNEL_APP_ID)`；优先级 openclaw > hermes > lark-channel（决策点 D2 可调）。
    `can_bind`、`_import_feishu_host_context` 接受三个 source。
  - 6c `lark_runtime.py`：`safe_lark_arguments` 的 bind 校验保持"source 必须等于绑定值"（恢复校验值后自动生效），无需改逻辑。
  - 6d `install.sh`/`install.ps1`：新增 `--target openclaw`（`~/.openclaw/skills`）与 `--target hermes`（`~/.hermes/skills`），加入目标表与帮助文本。
  - 6e 文档矩阵：`references/feishu.md` 绑定章节改为"OpenClaw/Hermes/Lark Channel 检测到才用 agent 绑定，source 与检测一致"；`setup.md` 增加各平台安装目标与检测信号表；README 更新平台支持列表。
  - 6f 测试：`_detect_agent_source` 各平台单测、`agent_source` 校验恢复测试、host-context 接受 openclaw/hermes 测试、安装器目标参数测试。
- **前置**：无；文件与 Step 2/4 有交集（manage.py），建议顺序执行避免冲突。
- **验收标准**：OpenClaw 环境变量下 `feishu-context` 返回 `agent_source_detected=openclaw` 且可绑定；Hermes 同理；原有 lark-channel 行为不变；`install.sh --target openclaw` 安装到 `~/.openclaw/skills`。
- **验证**：定向单测 + 全量 `pytest -q` + `validate_release.py` + 打包演练。
- **回退**：6a/6b 单点回退；文档可单独回退。

### Step 7：lark-cli Windows 安装指引（问题 5）

- **结果**：npm EBUSY 有明确处置路径，用户不再反复踩锁。
- **修改范围**：`references/feishu.md` 增加 Windows 小节：专用 `--prefix <state>/lark-cli` 目录、失败后重试（带退避）、占用检查（`tasklist`/Defender 排除项）、回退到全局 lark-cli 的验证步骤；可选提供 `scripts/install_lark_cli.ps1` 自动重试脚本。
- **前置**：无。
- **验收标准**：文档步骤可按 Windows 实机复现（人工验收项）；本地为纯文档改动，全量校验通过。
- **验证**：`pytest -q`、`validate_release.py`。
- **回退**：文档无风险。

### Step 8：全量验证与发布演练

- **结果**：最后一次修改后的完整证据。
- **动作**：全量 `pytest -q`；`validate_release.py`；两个打包脚本输出到 `/tmp` 并核对；`git diff --check`；`rg` 确认平台关键词无残留不一致。
- **验收标准**：全量通过（172 + 新增）、发布校验 exit 0、打包正常、diff 干净。
- **停止条件**：任一关键检查失败回到对应步骤，不进入交付声明。

## 3. 验证路径汇总

| 验收标准 | 验证方式 |
|---|---|
| 政策部分补丁不重置未提及字段 | 新增单测 + 全量回归 |
| Cookie 表格格式可解析、掩码 token 有定向提示 | 新增单测 |
| Windows 无管道中文配置 | 文档流程 + 本地文件流程测试（实机为人工项） |
| profile 自动纠正 | 新增 mock 测试 |
| Base 创建用相对 @fields 文件 | 新增参数断言测试 |
| 多平台检测/绑定/安装 | 各平台单测 + 安装器参数测试 |
| 无回归 | 全量 `pytest -q`、`validate_release.py`、打包演练 |

无法本地自动验证、需人工验收：Windows PowerShell 实机全流程、OpenClaw/Hermes 实机绑定、npm EBUSY 处置步骤。

## 4. 风险与未决项

- **行为变化（预期）**：恢复 OpenClaw/Hermes 支持是对上一批"仅 lark-channel"决定的回退，属于本计划明确目标；execution_policy 补丁不再重置未提及字段。
- **风险**：平台检测基于环境变量，多平台 env 并存时优先级需确定（D2）；`@fields` 相对路径依赖 `_run_lark` 固定 cwd，改动后需确认 dry-run 与真实调用一致。
- **残余限制**：非 lark-cli 原生支持平台（如 Cursor/Windsurf 等）无法用 `config bind`，只能走环境变量模拟或 existing/dedicated 绑定；真实 OpenClaw/Windows 流程仍需实机验收。
- **未执行验证**：Windows 实机、OpenClaw/Hermes 实机、真实飞书建库/授权链路。

## 5. 需用户决定的事项

- **D1 平台清单**：默认恢复 OpenClaw、Hermes，保持 lark-channel，并保留 Codex/Claude/Copilot 现有适配；是否需要再加其他平台（如 Cursor、Windsurf、国产 Agent），请点名。
- **D2 检测优先级与覆盖**：多平台环境变量并存时按 openclaw > hermes > lark-channel 判定是否可接受；是否允许用户在配置中显式指定 `agent_source` 覆盖检测。
- **D3 Windows 标准路径**：推荐采用 `--agent-file` 无管道方案（已存在，只需文档+host-context 扩展）；若坚持修管道，则投入在 run.ps1/PS5.1 兼容上，工作量更大。
- **D4 版本号**：本批属于新功能/修复，建议从 `2.1.0` 升 `2.2.0`（更新 plugin.json 与 CHANGELOG）；或保持 2.1.0 一并发布，由你定。

计划确认后按 `engineering-loop` 执行；D1/D2/D4 未确认前，Step 6 平台清单与版本动作先按推荐默认推进并在交付时说明。
