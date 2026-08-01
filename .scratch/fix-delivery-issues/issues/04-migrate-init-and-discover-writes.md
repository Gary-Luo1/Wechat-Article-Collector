# 04 — init_config 与 discover_only 写路径并发迁移

**What to build:** Agent stdin 配置补丁与只读发现流程对配置的写入同样并发安全，不再用旧状态覆盖其他进程的变更。

**Blocked by:** 02

**Status:** completed (2026-08-01, init_config/discover_only write paths migrated; full suite green)

- [ ] init_config 各 section 补丁与保存路径迁移到事务助手。
- [ ] discover_only 的两处配置保存迁移到事务助手。
- [ ] 补丁与发现行为不变，全量测试通过。
