# 参与贡献

感谢你愿意改进 mtbox。这是一个以个人 NAS 使用为主的单用户工具，范围有意保持克制。

## 开始之前

- 先读[架构说明](docs/architecture.md)与[开发说明](docs/development/setup.md)。
- 较大的功能或行为变更请先开 issue 说明场景与方案，避免做了不匹配的改动。
- 不接受以下方向：多用户/多租户、公开 SaaS、绕过站点风控、Cookie/网页登录自动化、
  与下载内容版权状态相关的功能、引入遥测或远程上报。
- 安全问题不要开公开 issue，按 [SECURITY.md](SECURITY.md) 私下报告。

## 开发

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest
```

- Python 3.11+；不引入前端构建链。
- 业务逻辑放 `services/`，Web/调度只做调用。
- 保持现有代码风格与注释密度；不要顺手重构无关代码。
- 新增依赖需要说明理由，并考虑镜像体积与供应链风险。
- 修改配置项时同步更新 `docs/configuration.md`、`.env.example` 与设置页。
- 修改行为时同步更新 `docs/usage.md`、`CHANGELOG.md` 的相关条目。

## 测试

- 修复 bug 请附回归测试；新功能至少覆盖成功与失败路径。
- 测试必须离线可跑，不读取真实 `.env`、数据库或调用真实 M-Team/qB/豆瓣。
- 不要在测试、日志或截图中写入真实 API Key、密码、Cookie 或用户数据。

## 提交与 PR

1. 从 `main` 开分支（如 `fix/task-delete`）。
2. 提交信息使用简短祈使句，例如 `fix: remove deleted tasks from task list`。
3. PR 描述写清：问题、方案、测试方式、是否有配置/迁移影响。
4. 确认 `python -m pytest -q -p no:warnings` 通过，且 `git status` 中没有 `.env`、
   `config/`、`*.db`、`images/`、`.build-image/`。
5. 维护者可能要求拆分范围或补充文档。

## 许可证

提交贡献即表示你同意以 [Apache License 2.0](LICENSE) 授权你的贡献。
本项目不要求签署 CLA。
