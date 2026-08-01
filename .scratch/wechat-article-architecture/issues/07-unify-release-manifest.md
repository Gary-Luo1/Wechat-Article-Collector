# 07 — 统一 release manifest 并最终验证

**What to build:** installer、packager、validator 和 README 对可发布文件使用同一份事实来源，并在干净 checkout 验证。

**Blocked by:** 02 — 隔离本地 queue 命令依赖; 03 — 深化 Article inbox module; 04 — 深化 Setup execution policy module; 05 — 统一 subscription resolution; 06 — 深化 Feishu target adapter.

**Status:** ready-for-agent

- [ ] release topology 不再引用缺失的 adapter 或 plugin。
- [ ] 完整测试和 release validation 有新的通过证据。

