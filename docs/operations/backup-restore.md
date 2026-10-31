# 备份与恢复

mtbox 的状态都在 `DATA_DIR` 里。备份目标是**能在另一台机器上完整恢复运行状态**。

## 1. 需要备份的内容

| 文件/目录 | 是否必需 | 说明 |
|---|---|---|
| `mtbox.db` | **必需** | 订阅、任务、候选、事件、运行时设置，以及设置页保存的 MT Key / qB 密码 / 通知配置（明文）。 |
| `secret.key` | **必需** | 会话签名密钥；缺失会导致所有登录会话失效，并可能影响加密状态。 |
| `.env` | **必需** | 管理密码、qB 地址/用户名、路径映射等部署配置。 |
| `backups/` | 可选 | 历史备份。 |
| `torrents/` | 可选 | 已下载的 `.torrent` 缓存；可重新获取。 |
| `logs/` | 可选 | 运行日志。 |

> `mtbox.db` 包含明文凭据，备份文件应按敏感数据保护：限制权限、加密存储，
> 不要放进公开网盘或 Git 仓库。

## 2. 在线备份数据库

推荐使用程序自带的 `backup` 命令，它使用 SQLite `VACUUM INTO`，在应用运行时
也能得到一致的快照：

```bash
# 本机运行
mtbox backup
# 指定输出
mtbox backup --output /secure/mtbox-2026-10-31.db

# Docker
docker exec mtbox mtbox backup
docker cp mtbox:/config/backups/mtbox-2026-10-31-120000.db ./mtbox-2026-10-31.db
```

默认输出到 `DATA_DIR/backups/mtbox-YYYYMMDD-HHMMSS.db`。确认命令输出「已备份到 …」
且文件大小非零后，再纳入你的备份系统（NAS 快照、restic、rsync 等）。

> 目前没有内置的定时备份任务；请用宿主机的定时任务/快照覆盖 `DATA_DIR`。
> 若使用文件级快照，建议在应用停止时进行，或同时快照 `-wal` / `-shm`。

## 3. 完整备份

最稳妥的方式是停止容器后备份整个 `DATA_DIR`：

```bash
docker compose stop mtbox
tar -czf mtbox-backup-$(date +%F).tar.gz -C /vol1/mtbox config
# 或使用 NAS 快照
docker compose start mtbox
```

同时备份 `.env`（单独加密保存）。

## 4. 恢复

1. 停止应用：

   ```bash
   docker compose down
   ```

2. 把备份的 `mtbox.db` 放回 `DATA_DIR/mtbox.db`，并恢复 `secret.key` 与 `.env`。
   如果恢复的是旧数据库，建议先删除同目录的 `mtbox.db-wal`、`mtbox.db-shm`
   （它们是旧数据库的未合并日志，不能与新文件混用）。

3. 修正权限（容器以 root 运行时通常无需处理）：

   ```bash
   chown -R 0:0 /vol1/mtbox/config      # 或实际运行用户
   chmod 600 /vol1/mtbox/config/mtbox.db /vol1/mtbox/config/secret.key
   ```

4. 启动并检查：

   ```bash
   docker compose up -d
   curl http://127.0.0.1:18080/health
   ```

5. 登录后查看仪表盘自检；确认订阅、任务、设置页的「已配置」状态和通知配置都回来了。

## 5. 只恢复设置或订阅

`mtbox.db` 是普通 SQLite 数据库。可以只查询/导出需要的表，例如：

```bash
sqlite3 mtbox.db "SELECT key, value FROM setting;"
sqlite3 mtbox.db ".dump subscription" > subscription.sql
```

但不要在应用运行时直接替换数据库文件。只恢复部分表时，注意外键关系
（订阅、任务、分集状态、候选相互关联），建议整体恢复。

## 6. 迁移到新机器

1. 在新机器准备相同（或可映射）的目录结构与 `.env`；
2. 恢复 `mtbox.db`、`secret.key`、`.env`；
3. 确认 `PATH_MAP_JSON` 与新机器的 qB 路径一致；
4. 启动并跑一次 `mtbox check-config --remote`。

## 7. 恢复后的凭据

- 如果旧数据库里保存过 MT Key / qB 密码，它们会随 `setting` 表一起恢复；
- 设置页只显示「已配置」，不会回显明文；
- 若怀疑备份泄露，立即在设置页清除覆盖并更换 M-Team 令牌与 qB 密码。
