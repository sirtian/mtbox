# 本地开发

## 环境要求

- Python 3.11 或 3.12（CI 覆盖这两个版本；推荐 3.11）
- 可选：Docker（构建镜像时）；本项目也提供不依赖 Docker daemon 的镜像打包脚本
- 一个可访问的 qBittorrent（只跑测试时不需要）

不需要 Node.js、数据库服务或前端构建工具。

## 初始化

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

`.env` 从**当前工作目录**读取。本地开发建议至少设置：

```env
MT_API_KEY=
QB_URL=http://127.0.0.1:8080
ADMIN_PASSWORD=dev-only-password
DRY_RUN=true
SCHEDULER_ENABLED=false
DATA_DIR=./config
DOWNLOAD_DIR=./downloads
LIBRARY_DIR=./media
PATH_MAP_JSON=[["/downloads","./downloads"]]
```

`PATH_MAP_JSON` 的右侧在真实部署中应是绝对路径；本地调试可使用相对路径对应的
绝对路径，避免映射结果取决于启动目录。

## 运行

```bash
mtbox check-config
mtbox serve --port 18080
```

默认 `DRY_RUN=true`：不会取种、不会删除任务，适合调试匹配与页面。
启动后访问 `http://127.0.0.1:18080/`。

其他命令：

```bash
mtbox --version
mtbox search "关键词" --mode tvshow --limit 10
mtbox backup
```

## 代码结构

见[架构说明](../architecture.md)。约定：

- 业务逻辑只放在 `services/`；Web 路由与调度器都只是调用方。
- 客户端异常统一为 `app/errors.py` 中的类型，路由层负责转成用户可读提示。
- 所有用户可见时间使用 `app/timeutil.py`；数据库存 UTC ISO。
- 新增设置：`settings_store.DEFAULTS` → `settings.html` → 路由校验 → 测试。
- 不要把真实 API Key、密码、数据库或日志写进代码/测试/文档。

## 提交前检查

```bash
python -m pytest -q -p no:warnings
mtbox check-config
```

涉及 Web 的改动请手动过一遍登录、订阅、候选、任务、设置页；涉及 Docker 的改动
请至少验证镜像能启动并通过 `/health`。

## 敏感数据

- `.env`、`config/`、`*.db`、`secret.key`、`images/`、`.build-image/` 已在
  `.gitignore` 中排除；提交前用 `git status` 再确认一次。
- 测试只使用假 Key；不要对真实 M-Team 账号跑会消耗预算的集成测试。
- 本地数据库可能包含明文凭据，不要复制到 issue 或聊天中。
