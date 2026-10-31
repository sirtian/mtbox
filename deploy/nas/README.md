# 极空间 NAS 部署说明

本文使用 `mtbox:0.5.0` 作为示例版本，实际请替换为你要部署的版本。

## 1. 获取并导入镜像

项目源码不包含预构建镜像（`images/` 被 `.gitignore` 忽略）。二选一：

**A. 使用发布包**：从仓库的 GitHub Releases 下载对应架构的
`mtbox-<版本>-<arch>.tar` 与 `SHA256SUMS`，先校验再导入：

```bash
sha256sum -c SHA256SUMS --ignore-missing
docker load -i mtbox-0.5.0-amd64.tar
```

**B. 本地构建**：在有 Docker 的机器（或极空间 SSH）上，于项目根目录执行：

```bash
docker build -t mtbox:0.5.0 .
# 需要搬到极空间时
docker save -o mtbox-0.5.0-amd64.tar mtbox:0.5.0
```

不确定架构时执行 `uname -m`：`x86_64` 用 amd64，`aarch64` / `arm64` 用 arm64。
也可以在极空间的 Docker/镜像管理页面选择「导入镜像」。

导入后自检：

```bash
docker images | grep mtbox
docker run --rm --workdir / --entrypoint python mtbox:0.5.0 -c "import app.main; print('mtbox import ok')"
docker run --rm --workdir / --entrypoint mtbox mtbox:0.5.0 --help
```

第一条应输出 `mtbox import ok`，第二条应打印 CLI 帮助。`--workdir /` 是为了
同时验证包路径和 `mtbox` 入口不依赖 `/app` 当前目录。

## 2. 准备配置

在 `deploy/nas/` 下创建 `.env`（复制同目录的 `.env.example`），
至少确认：

> `.env.example` 只是模板，Compose 不会读取它；真正生效的是同目录下的 `.env`。
> 改完 `PORT` 后必须重建容器（`docker compose up -d --force-recreate`），
> `docker restart` 不会重新注入环境变量。

```env
MT_API_KEY=你的馒头存取令牌
QB_URL=http://192.168.1.10:8080
QB_USERNAME=admin
QB_PASSWORD=你的qB密码
ADMIN_PASSWORD=你的管理端登录密码
DOWNLOAD_DIR=/downloads
LIBRARY_DIR=/media
DATA_DIR=/config
PATH_MAP_JSON=[["/downloads","/downloads"]]
DRY_RUN=true

# 极空间宿主机实际路径
MTBOX_CONFIG_PATH=/vol1/mtbox/config
MTBOX_DOWNLOADS_PATH=/vol1/downloads
MTBOX_LIBRARY_PATH=/vol1/media
# 容器内监听端口（默认 8080）与宿主机映射端口
PORT=8080
MTBOX_PORT=18080
```

注意：

- 先在极空间创建 `MTBOX_CONFIG_PATH` 等目录，或在图形界面选择已有目录。
- `/downloads` 在 mtbox 与 qBittorrent 容器里必须是同一个宿主机目录、同一个挂载点。
- 如果 qB 报告的路径不同，用 `PATH_MAP_JSON` 做映射，例如
  `[["/qb/data","/vol1/downloads"]]`。
- `QB_URL` 默认不要写容器内的 `127.0.0.1`：应写极空间的局域网 IP 加 qB 的映射端口，
  例如 `http://192.168.1.10:8080`；除非两个容器明确在同一个 Docker 网络里。
- qB 密码、MT Key 或 `ADMIN_PASSWORD` 如果包含 `$`，建议先留空/换密码；直接写在
  `.env` 时 Compose 可能把 `$...` 当成变量展开。管理端密码可以用单引号包住。
- `PORT` 是容器内监听端口，默认 8080；`MTBOX_PORT` 是宿主机映射端口，默认 18080。
  两者可以不同，健康检查会自动使用 `PORT`。
- 「豆瓣榜单」只读取公开页面，不需要账号或 Cookie；NAS 访问不了豆瓣时只影响
  榜单页（显示错误或旧缓存），不影响订阅检查与下载。可选参数
  `DOUBAN_TIMEOUT_SEC` / `DOUBAN_CACHE_SEC` 一般保持默认。
- 程序默认直连 MT/豆瓣/qB（`TRUST_ENV_PROXY=false`），不读取环境或系统代理；
  如果 NAS 上确实需要代理才能访问 M-Team，再把它设为 `true`。
- `DOWNLOAD_DIR` 与 `LIBRARY_DIR` 必须在同一存储池，否则硬链接会退化为复制。
- `.env` 不要打进镜像，也不要提交到仓库。

## 3. 启动

```bash
cd deploy/nas
docker compose up -d
docker compose logs -f --tail=100 mtbox
```

如果极空间只提供图形化 Docker 管理，也可以直接创建容器：

- 镜像：`mtbox:0.5.0`
- 端口：宿主机 `18080` → 容器 `8080`（容器端口可用 `PORT` 修改）
- 挂载：`/vol1/mtbox/config` → `/config`、`/vol1/downloads` → `/downloads`、
  `/vol1/media` → `/media`
- 环境变量：按上面的 `.env` 填入（或使用「环境变量文件」导入）
- 重启策略：`unless-stopped`

对应的命令行等价形式（注意：如果 `.env` 里的 `PORT` 不是 8080，
下面 `-p` 的容器端口也要改成同一个值；Compose 会自动跟随 `PORT`）：

```bash
docker run -d --name mtbox --restart unless-stopped \
  -p 18080:8080 --env-file .env \
  -e DOWNLOAD_DIR=/downloads -e LIBRARY_DIR=/media -e DATA_DIR=/config \
  -v /vol1/mtbox/config:/config \
  -v /vol1/downloads:/downloads \
  -v /vol1/media:/media \
  mtbox:0.5.0
```

健康检查：

```bash
curl http://127.0.0.1:18080/health
```

浏览器访问 `http://极空间IP:18080/`，用 `ADMIN_USERNAME` /
`ADMIN_PASSWORD` 登录，先看仪表盘的「启动自检」。

## 4. 测试建议

1. 保持 `DRY_RUN=true`，新建订阅，点「检查」，确认候选与评分正常。
2. 确认无误后改为 `DRY_RUN=false`，重启容器，再手动下载一条。
3. 检查 qB 是否收到任务、完成后是否硬链接到媒体库、极影视能否识别。
4. 确认稳定后再打开订阅的「自动抓取」。
5. 设置页可以更新 MT API Key / qB 密码；保存后立即生效，明文不会回显。
   设置页保存的凭据位于 `/config/mtbox.db`，请限制目录权限并加密备份。

MT API Key 与 qB 密码也可以先留空，登录后在设置页保存；也可以直接写在 `.env`。
设置页保存的值优先于 `.env`，清除后回退到 `.env`。
