# 05 — 统一 subscription resolution

**What to build:** discovery 与 direct ingest 使用同一套公众号匹配、歧义处理和未订阅发布者决策。

**Blocked by:** 02 — 隔离本地 queue 命令依赖; 04 — 深化 Setup execution policy module.

**Status:** ready-for-agent

- [ ] name、alias、biz 的匹配语义只有一处。
- [ ] ask、ingest_once、auto_subscribe 和显式确认保持正确。

