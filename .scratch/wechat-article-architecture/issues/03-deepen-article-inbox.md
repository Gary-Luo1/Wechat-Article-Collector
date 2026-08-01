# 03 — 深化 Article inbox module

**What to build:** Article inbox 统一管理 stable URL、查询、可逆状态和 sync 状态，调用者不再遍历 queue storage。

**Blocked by:** 02 — 隔离本地 queue 命令依赖.

**Status:** ready-for-agent

- [ ] inbox 查询与汇总通过公开 interface 完成。
- [ ] digest、标记、dismiss 和 restore 保持可验证的既有行为。

