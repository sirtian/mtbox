# 配置参考

mtbox 的配置分三层：

1. **环境变量 / `.env`**：部署级参数，启动时读取。
2. **运行时设置（SQLite `setting` 表）**：在 `/settings` 页面修改，立即生效。
3. **设置页保存的凭据**：MT API Key、qB 密码，覆盖 `.env` 中的同名值。

`.env` 由 `pydantic-settings` 从**当前工作目录**读取。用 `mtbox serve` 或
`uvicorn` 启动时，请在项目根目录（或 `deploy/nas/`）执行；Docker 部署使用
`env_file`，不受此限制。

## 1. 环境变量

### M-Team

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MT_API_KEY` | 空 | 存取令牌。控制台 → 实验室 → 存取令牌。也可在设置页保存。**必填**。 |
| `MT_API_BASE` | `https://api.m-team.cc/api` | Official Open API 根地址。 |
| `MT_USER_AGENT` | 浏览器 UA | 降低 Cloudflare 1010 概率；一般无需修改。 |
| `MT_SEARCH_MIN_INTERVAL_SEC` | `0` | 两次搜索之间的最小间隔（秒）；`0` 为不限速。设置页的「搜索最小间隔」保存后覆盖此项。 |
| `MT_SEARCH_DAILY_BUDGET` | `400` | 24h 搜索预算。设置页的「24h 搜索预算」保存后覆盖此项。 |
| `MT_DOWNLOAD_DAILY_BUDGET` | `60` | 24h 取种（`genDlToken`）预算。 |
| `MT_PROFILE_DAILY_BUDGET` | `200` | 24h 账号资料刷新预算（分享率护栏使用）。 |
| `MT_TIMEOUT_SEC` | `30` | M-Team 请求超时（秒）。 |

搜索页的分类、子分类和排序使用 M-Team 官方 `categories` / `sortField` /
`sortDirection` 参数，分类 ID 与排序枚举已在代码中校验。分类树与排序选项说明见
[使用指南 · 搜索页](usage.md#4-搜索页手动抓取)。

搜索最小间隔默认 `0`（不限速），只受 24h 预算约束；如果账号对频率较敏感，
可在设置页把「搜索最小间隔」调为 20 秒或更高。

### 豆瓣榜单

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOUBAN_TIMEOUT_SEC` | `15` | 公开榜单请求超时。 |
| `DOUBAN_CACHE_SEC` | `600` | 榜单缓存秒数。 |

豆瓣榜单只读取公开页面，不需要账号或 Cookie；访问失败只影响榜单页。榜单海报由
`/douban/cover` 代理加载：服务端带豆瓣 Referer 取图，仅允许官方图床
`img*.doubanio.com/view/photo/` 的 HTTPS 图片，不开放任意 URL 代理。

### qBittorrent

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QB_URL` | `http://127.0.0.1:8080` | qB Web UI 地址。NAS 上填局域网 IP + 映射端口，不要填 qB 容器内的 `127.0.0.1`。 |
| `QB_USERNAME` | 空 | qB 用户名。 |
| `QB_PASSWORD` | 空 | qB 密码。也可在设置页保存。 |
| `QB_SAVE_PATH` | `/downloads` | 添加任务时使用的保存路径（qB 视角）。 |
| `QB_CATEGORY` | `mtbox` | qB 分类。 |
| `QB_TAGS` | `mtbox` | qB 标签。 |
| `QB_TIMEOUT_SEC` | `15` | qB 请求超时。 |

### 路径

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOWNLOAD_DIR` | `/downloads` | 本程序看到的下载目录。 |
| `LIBRARY_DIR` | `/media` | 媒体库根目录。 |
| `DATA_DIR` | `/config` | 数据库、种子缓存、日志、`secret.key`、备份。 |
| `PATH_MAP_JSON` | `[["/downloads","/downloads"]]` | qB 路径前缀 → 本程序路径前缀，JSON 数组，最长前缀优先。 |

`PATH_MAP_JSON` 示例：

```env
# qB 报告 /downloads/xxx，本程序实际在 /vol1/downloads/xxx
PATH_MAP_JSON=[["/downloads","/vol1/downloads"]]
```

条目必须是 `[字符串, 字符串]` 且均为绝对路径；无法映射的路径会让整理失败并记日志。

### 管理端与行为

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | 管理端用户名。 |
| `ADMIN_PASSWORD` | 空 | 管理端密码。明文保存在 `.env`；请限制文件权限。**必填**。 |
| `SESSION_SECRET` | 空 | 会话签名密钥。留空时自动生成并保存到 `DATA_DIR/secret.key`。 |
| `PORT` | `8080` | Web 监听端口。 |
| `DRY_RUN` | `true` | `true` 时只搜索/生成候选，不取种、不删除任务。首次部署请保持开启。 |
| `SCHEDULER_ENABLED` | `true` | 是否启用进程内定时任务。 |
| `ORGANIZE_DELAY_SEC` | `15` | 任务完成后等待多久再整理（给 qB 落盘留时间）。 |
| `MIN_VIDEO_SIZE_MB` | `50` | 小于此大小的文件不视为正片。 |
| `LOG_LEVEL` | `INFO` | 日志级别。 |
| `TZ` | `Asia/Shanghai` | 界面与日志使用的时区。 |
| `TRUST_ENV_PROXY` | `false` | 是否读取系统/环境代理。NAS 直连时保持 `false`。 |

### Compose 专用变量

这些变量只由 `deploy/nas/docker-compose.yml` 读取：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MTBOX_PORT` | `18080` | 宿主机映射端口。 |
| `MTBOX_CONFIG_PATH` | `./config` | 宿主机配置目录 → `/config`。 |
| `MTBOX_DOWNLOADS_PATH` | `./downloads` | 宿主机下载目录 → `/downloads`。 |
| `MTBOX_LIBRARY_PATH` | `./media` | 宿主机媒体库目录 → `/media`。 |

## 2. 运行时设置

在 `/settings` 修改，保存在 `DATA_DIR/mtbox.db` 的 `setting` 表，立即生效。

| 键 | 默认值 | 说明 |
|---|---|---|
| `default_interval_min` | `30` | 新建订阅的默认检查间隔（分钟）。 |
| `auto_grab_default` | `false` | 新建订阅是否默认开启自动下载。 |
| `min_score` | `45` | 候选最低评分（0–100）。 |
| `max_candidates` | `20` | 每个订阅最多保留的自动忽略记录数。 |
| `candidate_ttl_days` | `7` | 候选保留天数。 |
| `season_pack_min_missing` | `3` | 合集至少能补多少缺集才优先于单集。 |
| `max_grabs_per_check` | `6` | 每次检查最多实际抓取的任务数。 |
| `new_episode_window_days` | `7` | 追新窗口（天）。 |
| `daily_download_budget_gb` | `50` | 24h 非免费下载量预算（GB，`0` 为不限）。 |
| `min_seed_hours` | `72` | 最短做种时长；满足后才允许清理。 |
| `max_seed_hours` | `336` | 最长做种时长，达到后即使分享率不足也可清理（`0` 为不强制）。 |
| `min_ratio` | `1.0` | 清理所需的最低分享率。 |
| `ratio_pause_nonfree` | `0.8` | 分享率低于此值只允许免费种子。 |
| `ratio_pause_all` | `0.6` | 分享率低于此值暂停全部抓取。 |
| `disk_pause_percent` | `90` | 磁盘使用率达到此值时暂停非免费抓取并加速清理。 |
| `movie_path_template` | 见设置页 | 电影归档路径模板。 |
| `tv_path_template` | 见设置页 | 剧集归档路径模板。 |
| `mode_map` | `{"movie":"normal","tv":"tvshow"}` | 订阅类型 → M-Team 搜索 `mode`。 |
| `category_map` | `{}` | 关键词/分类映射；`{}` 表示不校准，按搜索类型处理。 |
| `notify_config` | `{}` | 通知配置，见[使用指南](usage.md#通知)。 |
| `search_daily_budget` | 未写入 | 24h 搜索预算覆盖值。留空保存会删除该键并回退到 `MT_SEARCH_DAILY_BUDGET`。 |
| `search_min_interval_sec` | 未写入 | 搜索最小间隔覆盖值（秒，`0` 为不限）。留空保存会删除该键并回退到 `MT_SEARCH_MIN_INTERVAL_SEC`。 |

模板占位符：`{library}`、`{category}`、`{title}`、`{original_title}`、`{year}`、
`{season}`、`{episode}`、`{episode_end}`、`{resolution}`、`{video_codec}`、`{ext}`、`{group}`。
模板必须包含 `{title}`。未写 `{category}` 时，默认模板的 `Movies` / `TV` 顶层目录会被
订阅的「媒体库分类」替换。

## 3. 凭据与优先级

- `MT_API_KEY`、`QB_PASSWORD` 可由 `/settings` 覆盖 `.env`；页面只显示「已配置/未配置」，
  不回显明文，日志会对值做掩码。
- 设置页保存的值以**明文**存放在 `DATA_DIR/mtbox.db` 的 `setting` 表，与 `notify_config`
  中的 Webhook/SMTP 密码相同。请限制 `DATA_DIR` 访问权限并对备份加密。
- 清除设置页覆盖后回退到 `.env` 的值。
- `ADMIN_PASSWORD` 只从环境变量读取，不会写入数据库；修改后需重建容器或重启进程。

## 4. 路径与权限

容器默认以 root 运行，通过挂载目录的属主控制访问。若改为非 root 用户运行，需要保证：

- `DATA_DIR` 可写（数据库、日志、`secret.key`、备份）；
- `LIBRARY_DIR` 可写（创建硬链接与目录）；
- `DOWNLOAD_DIR` 可读（整理源文件），且与 `LIBRARY_DIR` 在同一文件系统时才能硬链接。

## 5. 校验配置

```bash
mtbox check-config           # 本地目录、模板、必需项
mtbox check-config --remote  # 额外测试 qB 登录与 M-Team /member/profile
```

`--remote` 会消耗 1 次账号资料预算；失败项会给出具体原因，启动自检也会在仪表盘展示。
