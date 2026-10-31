# 测试

## 运行

```bash
python -m pytest
# CI/发布前
python -m pytest -q -p no:warnings
```

测试默认不联网：M-Team、qBittorrent、豆瓣都通过注入的假客户端或
`httpx.MockTransport` 模拟。不要为测试申请真实令牌，也不要把真实响应（含账号信息）
粘进仓库。

## 布局

```
tests/
├─ conftest.py        公共 fixture
├─ helpers.py         Settings/数据库/种子构造、bencode 辅助
├─ test_web.py        登录、CSRF、页面、表单、任务删除等
├─ test_cli.py        CLI 命令与 --version
├─ test_mteam_client.py / test_qbittorrent_client.py / test_douban_client.py
├─ test_search_service.py / test_downloader.py / test_poller.py
├─ test_organizer.py / test_episodes.py / test_matcher.py
├─ test_ratelimit.py / test_seeding.py / test_profile_guard.py
├─ test_scheduler.py / test_selfcheck.py / test_notifier.py
├─ test_db.py / test_credentials.py / test_security.py
└─ test_release_name.py / test_bencode.py / test_app.py
```

## 约定

- 使用 `tests.helpers.make_settings` 指向临时目录（`tmp_path`），不要读写仓库里的
  `config/` 或真实数据库。
- Web 测试用 `TestClient(create_app(..., run_remote_checks=False))`，并先从 `/login`
  取 CSRF token；所有写操作都要带 token。
- 模拟 qB/M-Team 时保持异常类型与真实客户端一致，便于验证路由/服务的错误分支。
- 时间相关逻辑使用 `app/timeutil.py`，测试中用固定时间或数据库记录，不要依赖真实等待。
- 每个 bug 修复尽量配一条回归测试（例如任务删除后不再出现在任务列表）。

## 新增测试的检查点

- 是否覆盖失败路径（认证失败、预算耗尽、路径映射失败、文件缺失）？
- 是否会在无网络环境下通过？
- 是否留下临时文件或修改全局状态？
- 是否泄漏真实凭据或用户数据？

## 测试与发布

发布前必须全量通过；镜像构建脚本会读取 `pyproject.toml` 的版本号，
详见[发布流程](release.md)。
