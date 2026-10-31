# 发布流程

发布物包括：源码标签、双架构 docker-save 镜像、`SHA256SUMS`、变更日志。
`.env`、数据库、日志、真实凭据**永远不进入发布物**。

## 1. 版本号

采用语义化版本。发布前同时修改：

- `app/__init__.py` 的 `__version__`
- `pyproject.toml` 的 `[project] version`

测试会校验两者一致。再检查文档/部署文件里的固定版本示例：

```bash
grep -rn "0\.[0-9]\+\.[0-9]\+" README.md README.en.md deploy docs | grep -v CHANGELOG
```

需要更新的常见位置：`docker-compose.yml` 的 `image:`、README 的构建示例、
`deploy/nas/README.md` 的镜像名。

## 2. 首次公开发布前

替换仓库所有者占位符：

- `pyproject.toml` 的 `[project.urls]`
- `Dockerfile` 的 `org.opencontainers.image.source`

然后确认：

- `LICENSE` 年份与版权方正确；
- `.gitignore` 未失效：`git status --ignored` 中不应出现 `.env`、`config/`、`*.db`、
  `images/`、`.build-image/`；
- `git ls-files` 中没有密钥、数据库、备份或第三方参考代码。

## 3. 测试与自检

```bash
python -m pytest -q -p no:warnings
mtbox check-config
```

所有测试必须通过。涉及 Web 的版本请用临时数据目录启动一次，检查登录、页面与 `/health`。

## 4. 构建镜像

仓库提供不依赖 Docker daemon 的打包脚本（读取 `pyproject.toml` 版本号）：

```bash
python .build-image/build.py all          # amd64 + arm64
# 或单独构建
python .build-image/build.py amd64
python .build-image/build.py arm64
```

产物：

```
images/mtbox-<version>-amd64.tar
images/mtbox-<version>-arm64.tar
```

脚本会下载基础镜像层与 manylinux wheels，并做基本结构校验。若网络受限，可通过
`MTBOX_PIP_INDEX_URL` 指定可用的 PyPI 镜像。

## 5. 验证镜像

有 Docker 的机器：

```bash
docker load -i images/mtbox-<version>-amd64.tar
docker run --rm --workdir / --entrypoint mtbox mtbox:<version> --version
docker run --rm --workdir / --entrypoint python mtbox:<version> \
  -c "import app.main; print('mtbox import ok')"
```

没有 Docker 时，至少检查：

- 归档能被 `tar` 打开，包含 `manifest.json`、配置与层；
- 应用层包含 `app/`、`LICENSE`、`pyproject.toml`；
- 启动入口 `mtbox` 是 `/usr/local/bin/python` 脚本；
- 镜像内没有 `.env`、数据库、`images/`、`.build-image/`。

## 6. 校验值

`images/SHA256SUMS` 使用 GNU coreutils 格式，**LF 换行**，双空格 + `*`：

```text
<sha256> *mtbox-<version>-amd64.tar
<sha256> *mtbox-<version>-arm64.tar
```

新版本放在最前面。生成后自检：

```bash
cd images
sha256sum -c SHA256SUMS
```

Windows 上写文件时务必使用 `\n`，不要用默认的 `\r\n`，否则 `sha256sum` 会找不到文件。

## 7. 提交与标签

```bash
git add ...
git commit -m "release: v<version>"
git tag -a v<version> -m "mtbox v<version>"
git push origin main --tags
```

只提交源码、文档与配置模板；不要提交 `images/`（已在 `.gitignore` 中）。

## 8. GitHub Release

- 标题：`v<version>`
- 说明：取自 `CHANGELOG.md` 对应版本，附升级/备份提醒
- 附件：两个镜像 tar + `SHA256SUMS`
- 明确写出校验方法、支持的架构、最低配置与安全提示
- 不要附 `.env`、数据库、日志或任何真实账号信息

## 9. 发布后

- 用发布物在干净目录启动一次，访问 `/health` 并登录；
- 检查 `/docs`、`/openapi.json` 返回 404；
- 记录发布后的问题，下一版在 `CHANGELOG.md` 的 `Unreleased` 中补充。
