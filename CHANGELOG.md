# 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.5.0] - 2026-10-31

首个公开发布版本。

### Added

- Apache-2.0 `LICENSE`（Copyright 2026 mtbox contributors）与项目元数据
  （`pyproject.toml` 的许可证、作者、关键词、分类、项目链接）。
- 开源文档体系：配置参考、使用指南、Docker 部署、备份与恢复、故障排查、
  架构说明、合规说明、开发/测试/发布流程、中文文档索引与英文 README。
- 社区文件：`CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、
  `ROADMAP.md`、issue/PR 模板与 GitHub Actions CI。
- CLI `--version`，输出 `mtbox <版本>`。
- `.env.example` 补充 `MT_PROFILE_DAILY_BUDGET`。
- Docker 镜像 OCI 标签（标题、描述、许可证、源码地址），镜像内包含
  `pyproject.toml`、`README.md`、`LICENSE`。
- 测试：版本号与 `pyproject.toml` 一致性、`--version`、关闭的文档端点、
  未知页面返回 404。

### Changed

- README 重构为开源项目入口：功能、快速开始、关键约束、文档索引与免责声明。
- 设计文档增加实现状态标注：`/api/*` JSON API、网页登录到期提醒、M4 打磨项
  标注为**设计/未实现**。
- 极空间部署说明改为指导使用本地构建或 Release 镜像，不再假设仓库内存在 `images/`。
- 任务删除后统一软删除为 `deleted` 并从所有任务列表移除（含 `failed`、`candidate`），
  同时释放未整理分集、保留已整理分集（自 0.4.25 起）。

### Fixed

- 未知页面/禁用端点返回 404 页面时，HTTP 状态码误为 200。
- 删除任务后记录仍留在任务列表。

### Security

- 关闭 FastAPI 自动生成的 `/docs`、`/redoc`、`/openapi.json`；项目不提供公开 JSON API，
  避免在暴露实例上泄露内部路由结构。
- `.gitignore` 覆盖 `.env`、数据库、备份、日志、镜像构建产物与第三方参考代码。
- `Dockerfile` 构建上下文不再包含文档、测试与本地数据。

## [0.4.25] - 2026-10-31

### Fixed

- 从任务列表删除任务后，记录不再以 `failed` 形式残留：任意状态都软删除为
  `deleted`，从任务页（含「显示候选与已忽略」）移除；未整理的 `episode_state`
  被释放，后续检查可重新抓取，已整理分集保持不变。

## [0.4.24] - 2026-10-31

### Added

- 24h 搜索预算可在设置页运行时调整；留空保存会删除覆盖并回退到
  `MT_SEARCH_DAILY_BUDGET`，保存后立即生效，无需重启。

## 更早版本

0.4.0–0.4.23 为内部迭代版本，未逐版记录。主要能力包括：订阅与候选、自动抓取、
豆瓣榜单、集数发现与进度、分享率护栏、保种清理、通知、运行时设置与响应式界面。
