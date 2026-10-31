# 文档索引

本目录包含面向使用者、部署者和开发者的文档，以及早期设计稿。
设计稿描述的是**设计意图**，实现以代码与下列使用文档为准；设计稿中尚未实现的部分已标注。

## 使用与运维

| 文档 | 内容 |
|---|---|
| [配置参考](configuration.md) | 环境变量、运行时设置、路径映射、密钥存放 |
| [使用指南](usage.md) | 订阅、候选、任务、豆瓣榜单、定时任务、护栏、通知 |
| [Docker 部署](deployment/docker.md) | 构建/导入镜像、Compose、目录权限、反向代理、健康检查 |
| [极空间部署](../deploy/nas/README.md) | 极空间专用步骤与 `docker run` 等价命令 |
| [备份与恢复](operations/backup-restore.md) | 需要备份的文件、备份命令、恢复步骤、加密建议 |
| [故障排查](operations/troubleshooting.md) | 启动自检、qB 路径、硬链接、限流、登录锁定、通知 |
| [合规说明](compliance.md) | 站点规则、API 边界、隐私、法律与免责声明 |

## 开发

| 文档 | 内容 |
|---|---|
| [本地开发](development/setup.md) | 环境、运行、代码结构、提交前检查 |
| [测试](development/testing.md) | 测试布局、如何写测试、离线约束 |
| [发布流程](development/release.md) | 版本号、镜像构建、校验值、GitHub Release 检查清单 |

## 设计与架构

| 文档 | 内容 |
|---|---|
| [架构说明](architecture.md) | 当前实现的进程模型、模块、数据流、状态机 |
| [概要设计](概要设计.md) | 目标、非目标、总体架构（v0.1，含实现状态标注） |
| [详细设计](详细设计.md) | 表结构、接口、算法、调度（v0.2，含实现状态标注） |

## 阅读顺序建议

- 第一次部署：根目录 [README](../README.md) → [配置参考](configuration.md) → [Docker 部署](deployment/docker.md)
- 日常使用：[使用指南](usage.md)
- 出问题：[故障排查](operations/troubleshooting.md)，需要恢复数据时看 [备份与恢复](operations/backup-restore.md)
- 参与开发：[本地开发](development/setup.md) → [测试](development/testing.md) → [架构说明](architecture.md)
