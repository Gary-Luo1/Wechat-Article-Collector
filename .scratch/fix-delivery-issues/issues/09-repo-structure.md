# 09 — 仓库发布结构

**What to build:** 直接克隆仓库后，GitHub Actions 自动运行测试与发布校验，根 README 能引导用户找到项目并安装；源码归档携带的工作流保持不变。

**Blocked by:** 08

**Status:** completed (2026-08-01, root workflow parses; manual CI commands green)

- [ ] 仓库根存在可解析的 CI 工作流，工作目录指向项目子目录且路径正确。
- [ ] 根 README 提供项目入口与安装指引。
- [ ] 项目内工作流保留且与根工作流内容一致。
- [ ] 手动执行 CI 三步命令通过；归档相关测试通过。
