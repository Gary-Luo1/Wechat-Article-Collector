# 01 — 建立可复现测试基线

**What to build:** 维护者在本机任意干净环境都能复现「全量测试 188 通过、3 跳过」的基线，并把隔离验证环境的搭建命令固化下来，后续所有 ticket 使用同一验证方式。

**Blocked by:** None — can start immediately.

**Status:** done

- [x] 在隔离目录创建 venv 并安装运行与开发依赖（requests、beautifulsoup4、pytest）
- [x] 全量 pytest 通过：217 passed, 3 skipped（基线 188 + 新增 29）
- [x] 编译检查通过：compileall 无错误
- [x] 把环境搭建与验证命令记录到《架构深化实施计划（2026-08-02）》Step 0 中

> 完成记录（2026-08-02）：隔离 venv `/tmp/was-venv-baseline`；
> `pytest -q -p no:cacheprovider` → 217 passed, 3 skipped；compileall 与 release validation 通过。
