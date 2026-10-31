# mtbox

轻量的 **M-Team（馒头）订阅下载系统**，面向 NAS + Docker + qBittorrent。
服务端渲染、单进程、SQLite，没有前端构建链，也不依赖 TMDB 等海外元数据服务。

**中文** | [English](README.en.md)

> **免责声明**：本项目与 M-Team、豆瓣、qBittorrent、极空间均无关联，也未获其背书。
> 请只在你有权使用的账号与网络环境中运行，遵守站点规则和当地法律法规。
> 本项目不提供、不托管、不分发任何影视资源。详见 [合规说明](docs/compliance.md)。

## 功能

- **订阅追剧**：按标题/年份/季号匹配 M-Team 发布，自动识别集数，补齐缺失集。
- **候选与人工确认**：默认只生成候选并给出评分与规则明细；确认后再下载。
- **自动下载（可选）**：每个订阅可单独开启，自动选择最优种子组合（必要时多条补齐）。
- **增强搜索**：按 M-Team 官方分类/子分类筛选，支持服务端排序、优惠筛选、分页与空关键词浏览；
  结果同时展示人类可读标题与发布文件名。
- **豆瓣榜单**：浏览剧集/电影/综艺热门，带海报卡片，按年份、类别筛选，一键预填订阅；
  只读公开页面，不需要账号；海报经本机窄代理加载，不从豆瓣拉取账号数据。
- **下载与整理**：推送到 qBittorrent，轮询进度，完成后硬链接到媒体库并重命名，交给极影视刮削。
- **分享率护栏**：按账号分享率与磁盘水位自动切换「仅免费」或暂停抓取。
- **保种清理**：达到做种时长/分享率后清理，磁盘紧张时优先回收。
- **通知与日报**：企业微信、Server酱、SMTP；抓取、整理、护栏切换、错误与每日摘要。
- **运行时设置**：评分、预算、护栏、模板、通知都可在网页里改，无需重启。
- **安全边界**：只使用 M-Team 官方 Open API 的 `x-api-key`；不使用 Cookie，不调用登录接口；
  API Key 与密码不回显、不写日志、不拼进下载 URL。

## 工作方式

```
浏览器 ──► FastAPI (Jinja2 服务端渲染) ──► SQLite
                    │
                    ├─► M-Team Open API   （搜索 / 取种，受预算与间隔限制）
                    ├─► qBittorrent       （添加任务 / 查询进度 / 删除）
                    └─► 硬链接 + 重命名    ──► 媒体库（极影视刮削）

APScheduler（进程内）──► 与网页共用同一组服务
```

订阅匹配不依赖 TMDB；豆瓣只用于浏览榜单和预填，媒体刮削由你的媒体服务器完成。

## 快速开始

### 方式一：Docker Compose（推荐）

获取源码后：

```bash
cd mtbox/deploy/nas
cp .env.example .env
# 编辑 .env：MT_API_KEY、QB_URL、QB_PASSWORD、ADMIN_PASSWORD、宿主机路径
docker compose up -d
```

浏览器打开 `http://NAS_IP:18080/`，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录，
先看仪表盘的「启动自检」。容器内监听 `PORT`（默认 8080），宿主机端口由
`MTBOX_PORT`（默认 18080）控制。

没有现成镜像时，在项目根目录构建：

```bash
docker build -t mtbox:0.6.3 .
```

也可以使用 `docker save` / `docker load` 在离线机器之间搬运镜像，详见
[部署文档](docs/deployment/docker.md) 和 [极空间部署说明](deploy/nas/README.md)。

### 方式二：本地运行

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env               # 编辑 MT_API_KEY / QB_* / ADMIN_PASSWORD
mtbox check-config                 # 不联网的配置自检
mtbox serve                        # 默认 http://127.0.0.1:8080/
```

`python -m pytest` 运行测试；`mtbox --version` 查看版本。

### 必填配置

| 变量 | 说明 |
|---|---|
| `MT_API_KEY` | 馒头控制台 → 实验室 → 存取令牌。**只使用 Access Token**；也可登录后在设置页填写。 |
| `QB_URL` / `QB_USERNAME` / `QB_PASSWORD` | qBittorrent Web UI。NAS 上不要填 qB 容器内的 `127.0.0.1`，应填局域网 IP + 映射端口。 |
| `ADMIN_PASSWORD` | 管理端登录密码。明文保存在 `.env`，请限制权限，不要提交。 |
| `DOWNLOAD_DIR` / `LIBRARY_DIR` / `DATA_DIR` | 容器内路径，默认 `/downloads`、`/media`、`/config`。 |
| `PATH_MAP_JSON` | qB 报告的路径前缀 → 本程序看到的路径前缀。 |

完整参数见 [配置参考](docs/configuration.md)。

### 第一次使用

1. 在「订阅」→「新建订阅」填写剧名/电影名，建议使用中文名并补全年份、季号。
2. 点订阅行的「检查」生成候选；在「候选」页查看评分与最优组合。
3. 保持 `DRY_RUN=true` 先观察。确认无误后改为 `DRY_RUN=false` 并重启，再下载。
4. 任务完成后回到「任务」页点「整理待处理」，检查媒体库里的文件能否被极影视识别。
5. 稳定后再对具体订阅开启「自动下载」。详细流程见 [使用指南](docs/usage.md)。

## 重要约束

### qBittorrent 与 mtbox 必须挂载同一个下载目录

两个容器要看到**同一个宿主机目录**，并且挂载点一致（默认 `/downloads`）：

```yaml
# qBittorrent 与 mtbox 都这样挂载
volumes:
  - /vol1/downloads:/downloads
```

路径不一致时用 `PATH_MAP_JSON` 映射，左侧是 qB 报告的路径，右侧是 mtbox 能读到的路径，
最长前缀优先：

```env
PATH_MAP_JSON=[["/downloads","/vol1/downloads"],["/qb/data","/vol1/downloads"]]
```

### 硬链接要求同一存储池

整理默认使用硬链接，`DOWNLOAD_DIR` 与 `LIBRARY_DIR` 必须位于同一文件系统/存储池。
跨池时启动自检会告警，整理会退化为复制（占用双份空间）。

### 只部署在内网，单 worker

- 管理端是单用户密码认证，**不要直接暴露到公网**。需要远程访问时请用 VPN，
  或在反向代理后加 TLS 与额外认证。
- 限速器、调度锁、登录节流都是进程内状态，必须以 `--workers 1` 运行。

## 文档

| 文档 | 内容 |
|---|---|
| [配置参考](docs/configuration.md) | 全部环境变量、运行时设置、路径映射 |
| [使用指南](docs/usage.md) | 订阅、候选、任务、榜单、定时任务、护栏、通知 |
| [Docker 部署](docs/deployment/docker.md) | 构建/导入镜像、Compose、权限、反向代理 |
| [极空间部署](deploy/nas/README.md) | 极空间专用步骤与等价 `docker run` |
| [备份与恢复](docs/operations/backup-restore.md) | 数据库、`secret.key`、`.env` 的备份与恢复 |
| [故障排查](docs/operations/troubleshooting.md) | 自检失败、qB 路径、硬链接、限流、登录锁定 |
| [架构说明](docs/architecture.md) | 进程模型、模块、数据流、状态机 |
| [合规说明](docs/compliance.md) | 站点规则、API 使用边界、隐私与法律提示 |
| [开发说明](docs/development/setup.md) | 本地开发、测试、发布流程 |
| [设计文档](docs/README.md) | 概要设计与详细设计（含实现状态标注） |

## 开发

```bash
pip install -e ".[dev]"
python -m pytest
mtbox check-config
```

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按
[SECURITY.md](SECURITY.md) 私下报告，不要开公开 issue。

## 许可

[Apache License 2.0](LICENSE)。Copyright 2026 mtbox contributors。

第三方名称与商标归各自所有者。本项目不包含任何第三方影视资源或站点数据。
