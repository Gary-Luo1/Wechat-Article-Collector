# 03 — manage.py 写路径并发迁移

**What to build:** 所有通过 manage 命令读写配置的路径（目标选择、身份、App、经理、host 上下文、执行政策、重置、禁用等）在并发运行时不再互相覆盖。

**Blocked by:** 02

**Status:** completed (2026-08-01, all manage write paths migrated; full suite green)

- [ ] manage.py 全部「读配置→修改→保存」路径迁移到事务助手。
- [ ] 各管理命令行为不变（既有测试覆盖）。
- [ ] 代码中不再存在未持锁的读改写模式（搜索核对）。
- [ ] 全量测试通过。
