# 08 — 发布门禁修复

**What to build:** 发布校验通过：仓库无 .DS_Store 杂物、版本全仓库统一为 2.1.0、校验器能发现未来版本漂移，打包产物命名正确且不含杂物。

**Blocked by:** None — can start immediately.

**Status:** completed (2026-08-01, validate_release exit 0; archives 2.1.0; tests/test_release_gate.py 3 passed)

- [ ] 删除全部 4 个 .DS_Store，并在忽略规则中防止再次提交。
- [ ] 插件清单版本为 2.1.0，与 Changelog 头一致。
- [ ] 发布校验通过；版本漂移检查生效（人为改回旧版本时校验失败）。
- [ ] 两个打包脚本产出 2.1.0 归档，解压清单无 .DS_Store。
- [ ] 全量测试通过。
