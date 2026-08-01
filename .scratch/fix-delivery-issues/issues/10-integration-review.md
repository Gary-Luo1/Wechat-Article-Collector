# 10 — 最终集成验证与统一审阅

**What to build:** 全部修复合流后，项目达到可发布交付标准：全量测试、发布校验、打包演练、diff 审查全部通过，遗留限制如实记录。

**Blocked by:** 01-09

**Status:** completed (2026-08-01, unified review: 165 passed/3 skipped, validate exit 0, diff clean, no openclaw/hermes)

- [ ] 全量测试通过（148 + 新增，3 跳过）。
- [ ] 发布校验通过，打包产物正确。
- [ ] diff 审查：无无关改动、无半截实现、未触碰用户已有修改。
- [ ] 上轮成果不回归（无 OpenClaw/Hermes 残留）。
- [ ] 无法在本机验证的项（真实微信/飞书、Windows、GitHub Actions）如实列为交付限制。
