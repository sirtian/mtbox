# 架构说明

本文描述**当前实现**。更完整的设计意图见[概要设计](概要设计.md)与[详细设计](详细设计.md)；
设计稿中未实现的部分已在其顶部标注。

## 1. 运行模型

- 单进程：FastAPI + Uvicorn，必须 `workers=1`。
- Web 路由是同步函数，由 Starlette 放入线程池执行，避免阻塞事件循环。
- APScheduler（`BackgroundScheduler`）在进程内运行，与网页共用同一组服务对象。
- 状态全部在 SQLite（WAL）与进程内存中：限速器、调度锁、登录节流、搜索锁都是进程内状态。
- 没有 Redis、消息队列、独立 worker，也没有前端构建链。

## 2. 模块

```
app/
├─ main.py          create_app()、lifespan、日志、/health
├─ cli.py           check-config / search / backup / serve
├─ config.py        pydantic-settings 环境配置
├─ db.py            SQLite 连接、事务、迁移
├─ migrations.py    SCHEMA_V1..V5
├─ models.py        dataclass 行模型
├─ security.py      管理密码、CSRF、登录限速
├─ redaction.py     日志敏感值掩码
├─ timeutil.py      UTC ISO 时间与本地展示
├─ clients/
│  ├─ mteam.py          M-Team Open API（x-api-key）
│  ├─ qbittorrent.py    qB Web API
│  ├─ douban.py         豆瓣公开榜单
│  ├─ bencode.py        .torrent 解析、info_hash
│  └─ release_name.py   发布名解析（季/集/分辨率/编码/字幕）
├─ services/
│  ├─ subscriptions.py  订阅 CRUD 与进度
│  ├─ search_service.py 搜索、过滤、评分、候选
│  ├─ downloader.py     取种、推 qB、状态迁移
│  ├─ poller.py         qB 进度同步
│  ├─ organizer.py      硬链接、命名、归档
│  ├─ episodes.py       集数发现与映射
│  ├─ matcher.py        标题归一化、硬过滤、评分
│  ├─ storage.py        路径映射与文件选择
│  ├─ seeding.py        保种清理
│  ├─ profile_guard.py  分享率护栏
│  ├─ notifier.py       企业微信/Server酱/SMTP
│  ├─ scheduler.py      APScheduler 任务
│  ├─ ratelimit.py      最小间隔与 24h 预算
│  ├─ settings_store.py 运行时设置
│  ├─ selfcheck.py      启动自检
│  └─ credentials.py    设置页凭据覆盖
└─ web/
   ├─ routes.py       页面路由与表单处理
   ├─ templates/      Jinja2 模板
   └─ static/         单套深色主题 CSS + 少量原生 JS
```

## 3. 请求与数据流

### 网页

```
浏览器 ──HTTP──► routes.py ──► services/* ──► SQLite
                                  ├─► MTeamClient ──► api.m-team.cc
                                  ├─► QBittorrentClient ──► qB Web API
                                  └─► DoubanClient ──► 豆瓣公开榜单
```

页面使用服务端渲染 + 表单 POST + 303 重定向；所有写操作校验 CSRF。
数据库使用短事务；耗时的网络请求不在事务内执行。

### 定时任务

`Scheduler` 依次注册：

| 任务 | 作用 |
|---|---|
| `tick_subscriptions` | 到期订阅检查（与手动检查共用搜索锁） |
| `poll_torrents` | qB 进度同步 |
| `organize_pending` | 整理完成任务 |
| `profile_guard` | 刷新分享率快照并切换护栏 |
| `seed_cleanup` | 清理达标做种 |
| `prune` | 清理过期候选/预算/事件 |
| `daily_digest` | 每日摘要通知 |

## 4. 数据模型

| 表 | 作用 |
|---|---|
| `subscription` | 订阅、状态、间隔、自动下载、媒体库分类 |
| `torrent` | 任务/候选，含 `state`、qB hash、错误、路径 |
| `episode_state` | 订阅 × 季 × 集 → 任务/状态，防重复下载 |
| `api_call` | API 调用记录，用于 24h 预算 |
| `account_snapshot` | 分享率/流量快照 |
| `job_run` | 定时任务运行结果 |
| `event_log` | 用户可见事件流 |
| `setting` | 运行时设置与设置页凭据覆盖 |
| `schema_meta` | 迁移版本 |

迁移在启动时按 `SCHEMA_V1..V5` 顺序执行，当前 `LATEST_VERSION=5`
（V4 增加 `content_path`，V5 增加 `organized_paths`）。

## 5. 任务状态机

```
candidate ──grab──► grabbed ──► downloading ──► completed ──► organized
   │                   │              │             │
   │                   └─失败─► failed │             └─► organize_failed
   └─ignore─► ignored                  └─失败─► failed
```

- `failed → candidate` 可人工重试；自动忽略的 `ignored` 可在后续搜索中复活；
- 人工忽略是终态；
- 用户从任务页删除是**任意状态 → `deleted`**：软删除、从所有任务视图移除，
  同时删除 qB 种子与文件，并释放未整理的 `episode_state`。

## 6. 限速与预算

- 搜索：`MT_SEARCH_MIN_INTERVAL_SEC`（默认 0，即不限速）最小间隔 + 24h 搜索预算；
  两者均可由设置页的 `search_min_interval_sec` / `search_daily_budget` 覆盖，
  保存后立即生效；留空恢复 `.env` 默认。
- 取种、账号资料各有独立 24h 预算。
- `api_call` 表持久化调用记录，重启不会重置预算。
- 手动检查与自动检查共用搜索锁，不会并发搜索。

## 7. 路径与整理

- qB 与本地两套路径命名空间通过 `PATH_MAP_JSON` 映射，最长前缀优先。
- 整理优先使用硬链接；跨文件系统时退化为复制并告警。
- 任务记录的文件缺失时，会尝试 qB 的 `content_path` 与同级唯一同名文件兜底。
- 归档路径由设置页模板渲染，写入 `organized_paths`，支持重新整理时清理旧链接。

## 8. 安全边界

- 只使用 M-Team 官方 Open API 的 `x-api-key`；不使用 Cookie，不调用登录接口。
- 管理端单用户密码 + Session Cookie + CSRF；连续失败锁定。
- 敏感值在日志中掩码；设置页不回显明文。
- 下载的 `.torrent` 视为不可信输入：限制大小/文件数，只解析元数据，不执行内容。
- 不启用 FastAPI 的 `/docs`、`/redoc`、`/openapi.json`。

## 9. 扩展点

- 新通知渠道：`services/notifier.py`。
- 新评分/过滤规则：`services/matcher.py`。
- 新客户端：`clients/`，保持异常模型统一为 `app/errors.py`。
- 新设置项：`settings_store.DEFAULTS` + `settings.html` + 校验逻辑。
