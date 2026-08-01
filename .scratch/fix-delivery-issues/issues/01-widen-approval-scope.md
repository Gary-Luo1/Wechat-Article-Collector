# 01 — 授权失效范围扩展

**What to build:** 任何改变飞书授权实际覆盖范围的变化——绑定方式、Agent 来源、期望用户、CLI 配置、字段映射——都会使已确认的执行政策失效，不再沿用旧授权执行自动同步或自动建库。

**Blocked by:** None — can start immediately.

**Status:** completed (2026-08-01, tests/test_execution_policy.py 4 passed)

- [ ] 新增的 5 个范围字段任一变化即触发授权失效；现有 7 个字段行为不变。
- [ ] 未列入范围的无关字段变化不触发失效。
- [ ] 相关单元测试通过，全量测试通过（基线 148 passed, 3 skipped + 新增）。
