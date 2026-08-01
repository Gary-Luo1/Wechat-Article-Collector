# 07 — process_pending 写路径迁移与映射失效

**What to build:** 完成/同步流程对配置的写入（订阅添加、字段映射保存）并发安全；字段映射内容变化时已确认授权同步失效，不再沿用旧授权写入新映射。

**Blocked by:** 02；05（同一文件，须在其后执行）

**Status:** completed (2026-08-01, tests/test_feishu_mapping.py 1 passed)

- [ ] process_pending 的配置写路径迁移到事务助手。
- [ ] 保存字段映射且内容变化时执行政策失效；内容不变则不失效。
- [ ] 既有同步/评分/订阅行为不变，全量测试通过。
