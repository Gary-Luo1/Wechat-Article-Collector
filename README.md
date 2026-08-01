# Wechat Article Skill Develop

微信公众号文章发现与飞书多维表格工作流。本仓库包含一个可安装的 Codex
Skill（`wechat-article-subscriber`）：自动发现订阅公众号的新文章、按规则评分、
管理待读队列，并可选同步到飞书多维表格。

## 项目位置

- 项目源码与文档：[Wechat Article Skill Develop/](./Wechat%20Article%20Skill%20Develop/)
- 使用说明：[项目 README](./Wechat%20Article%20Skill%20Develop/README.md)
- 变更记录：[CHANGELOG](./Wechat%20Article%20Skill%20Develop/CHANGELOG.md)

## 快速开始

按项目 README 的安装说明使用 `install.sh`（macOS/Linux）或 `install.ps1`
（Windows）安装。安装后通过 Skill 的 `manage` 命令完成微信 Cookie、订阅源和
飞书目标配置。

## 开发与验证

本仓库的 GitHub Actions 会在项目目录中自动运行全量测试与发布校验；本地可手动
执行等价命令（见项目 README 的开发章节）。
