# Docker 部署

mtbox 是单进程应用，容器内监听 `PORT`（默认 8080），数据全部放在 `DATA_DIR`
（默认 `/config`）。它需要能访问 qBittorrent Web UI 和 M-Team API。

## 1. 准备

- 一台能运行 Docker 的 NAS 或 Linux 主机；
- 已运行的 qBittorrent（Web UI 可访问）；
- M-Team 存取令牌；
- 一个下载目录和一个媒体库目录（建议同一存储池，才能硬链接）。

## 2. 获取镜像

### 本地构建

```bash
docker build -t mtbox:0.5.0 .
```

镜像不包含 `.env`、数据库或任何凭据；`docs/`、`tests/` 也不会进入构建上下文
（见 `.dockerignore`）。

### 离线导入 / 导出

没有镜像仓库时，可以在有 Docker 的机器上构建后导出：

```bash
docker save -o mtbox-0.5.0-amd64.tar mtbox:0.5.0
# 复制到目标机器
docker load -i mtbox-0.5.0-amd64.tar
```

如果项目提供了 GitHub Release 镜像包，优先使用对应架构的归档，并校验 `SHA256SUMS`：

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

## 3. Compose 部署

仓库内提供 [deploy/nas/docker-compose.yml](../../deploy/nas/docker-compose.yml)，
它只引用镜像，不要求 NAS 构建源码。最小示例：

```yaml
services:
  mtbox:
    image: mtbox:0.5.0
    container_name: mtbox
    restart: unless-stopped
    env_file: .env
    environment:
      DOWNLOAD_DIR: /downloads
      LIBRARY_DIR: /media
      DATA_DIR: /config
      PORT: 8080
    ports:
      - "18080:8080"
    volumes:
      - /vol1/mtbox/config:/config
      - /vol1/downloads:/downloads
      - /vol1/media:/media
```

```bash
cp .env.example .env
# 编辑 .env
docker compose up -d
docker compose logs -f --tail=100 mtbox
```

`.env.example` 只是模板，Compose 读取的是同目录的 `.env`。修改环境变量后需要
`docker compose up -d --force-recreate`；`docker restart` 不会重新注入环境变量。

## 4. 目录与权限

| 容器路径 | 用途 | 权限 |
|---|---|---|
| `/config` | SQLite、种子缓存、日志、`secret.key`、备份 | 可写 |
| `/downloads` | qB 下载目录 | 可读；删除任务时 qB 负责删文件 |
| `/media` | 媒体库 | 可写（建硬链接/目录） |

- `/downloads` 必须与 **qBittorrent 容器**挂载同一个宿主机目录，并尽量使用相同挂载点。
  路径不同则配置 `PATH_MAP_JSON`。
- `/downloads` 与 `/media` 位于同一文件系统时整理使用硬链接，否则退化为复制。
- 镜像默认以 root 运行。若你改为非 root 用户，请确保上述目录对该用户可写，
  并保证 `secret.key` 的属主一致，否则每次重启会生成新密钥、使登录会话失效。

## 5. 端口与健康检查

- 容器内端口由 `PORT` 控制；Compose 示例用 `MTBOX_PORT` 控制宿主机端口，
  默认映射 `18080:8080`。
- `GET /health` 无需登录，返回 JSON：数据库、调度器、qB 连通性、预算用量。
  镜像自带 `HEALTHCHECK`，也用 `PORT` 拼接地址。
- 不要把容器端口直接暴露到公网。

## 6. 反向代理

需要 HTTPS 或统一入口时，把 mtbox 放在反向代理后。示例（Caddy）：

```caddy
mtbox.example.lan {
    reverse_proxy 127.0.0.1:18080
}
```

Nginx 示例：

```nginx
server {
    listen 443 ssl;
    server_name mtbox.example.lan;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

注意：

- 应用不设置 `Secure` Cookie（`https_only=False`），请只在可信网络使用；
  若通过公网访问，务必在代理层加 TLS 与额外访问控制（如 VPN、mTLS、基本认证）。
- 登录限流基于进程内状态；多副本部署没有意义，也会破坏限速与调度锁。

## 7. 升级与回滚

1. 先备份 `DATA_DIR` 下的 `mtbox.db`、`secret.key`（见[备份与恢复](../operations/backup-restore.md)）。
2. 停止容器：`docker compose down`。
3. 导入/构建新镜像，更新 Compose 中的镜像标签。
4. `docker compose up -d`。
5. 检查仪表盘自检与 `/health`。

数据库迁移在启动时自动执行；升级前保留旧数据库副本，回滚时连同旧镜像一起恢复。

## 8. 常见部署问题

见[故障排查](../operations/troubleshooting.md)。
