# 02 — 隔离本地 queue 命令依赖

**What to build:** 用户可在没有文章解析器或 Feishu CLI 时执行本地 list、inbox、dismiss、restore 和 digest-plan。

**Blocked by:** 01 — 建立可复现基线并拆分当前失败.

**Status:** ready-for-agent

- [ ] queue-only CLI 命令不导入可选外部 adapter。
- [ ] 依赖检查仅阻止真正需要该依赖的命令。

