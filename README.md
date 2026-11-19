# QMG Mercury · 战场军需官重建

本仓库是 2026-11-19 启动的独立重建基线。目前提供可运行的 Node.js / TypeScript 基础环境、启动页、自动检查，以及旧项目和开源 AI 的参考索引。游戏规则、正式内容、桌面打包和 AI 尚未在新代码中实现。

## 开始运行

需要 Node.js **24.12+（24.x）**、npm 11 和 Git。

```powershell
npm ci
npm run check
npm run dev
```

打开 [本地启动页](http://127.0.0.1:5173)。也可运行 `powershell -File tools/setup.ps1` 完成安装和检查。`npm run build` 后用 `npm start` 启动编译版本。

## 项目入口

| 位置 | 用途 |
| --- | --- |
| [旧项目分析](docs/legacy-analysis.md) | 实际进度、可复用模块、未完成事项和资源 |
| [重建顺序](docs/restart-plan.md) | 新项目边界与后续最小里程碑 |
| [开发环境说明](docs/development.md) | 运行、检查、Git 与问题排查 |
| [资源库索引](resources/README.md) | Forge、TripleA 与本地旧库导航 |
| [来源锁定清单](resources/sources.lock.json) | 347 个上游文件的提交、路径、体积和 Git blob 校验值 |
| `src/`、`web/`、`tests/` | 新项目代码、启动页和检查 |
| `legacy/2026-11-19/` | 本机原样冻结的完整旧工作目录，不上传 GitHub |
| `resources/upstream/` | 下载的上游原始源码，不参与新程序运行或构建 |

## 常用命令

```powershell
npm run doctor          # 环境、Git、内存及参考资料状态
npm run legacy:verify   # 核验本机 1,725 个旧文件是否仍原样保留
npm run refs:download   # 资源足够时下载/补齐锁定的 AI 源码
npm run refs:verify     # 严格核验本机参考源码；缺文件会失败
npm run check          # 类型、构建、服务检查与目录/资料边界
```

新克隆不含本机旧归档或第三方下载；`check` 会明确报告缺失并跳过其字节核验。完整本地核验使用上述两个严格命令。源码下载保留 512 MiB 可用物理内存和 30 GiB 可用磁盘保护，触线保留已有文件，可再次运行补齐。

GitHub：[MercuryNestPasUnRobot/qmg-mercury](https://github.com/MercuryNestPasUnRobot/qmg-mercury)。旧 Git 分支和标签仅留在本机，不能将 `--all`、`--mirror` 或旧归档标签推送到这个新公开仓库。
