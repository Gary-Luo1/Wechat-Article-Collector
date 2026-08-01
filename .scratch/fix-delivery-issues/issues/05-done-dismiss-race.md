# 05 — done/dismiss 并发竞态修复

**What to build:** 已撤回的文章不会被完成、不会被同步到飞书，也不会因并发竞态产生缺少评分数据的飞书记录；对其执行 done 得到清晰错误而非静默成功。

**Blocked by:** None — can start immediately.

**Status:** completed (2026-08-01, queue_helpers/process_pending + 3 regression tests in tests/test_core.py)

- [ ] 对已撤回条目执行完成操作明确报错，不再返回该条目。
- [ ] done 命令对已撤回文章返回非零退出码与清晰错误，不调用飞书写入。
- [ ] 同步前防御性校验处置状态，已撤回绝不进入同步。
- [ ] 回归测试覆盖「撤回后 done」场景；正常完成、撤回/恢复、失败留待重试行为不变。
- [ ] 全量测试通过。
