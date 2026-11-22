# QMG Mercury · 战场军需官重建

本仓库是 2026-11-19 启动的独立重建项目。Python Core 支持统一 Token 位置、地图与补给查询、串行结算、阻塞选择、能力触发、日志重放及确定性单步 AI 评价。浏览器验收台可运行原创机制场景及本地转录卡库，包含空军、暗置响应、回合末对象恢复和存档续跑。整套转录仅留本地；完整产品界面仍属客户端阶段。

## 开始运行

需要 Node.js **24.12+（24.x）**、npm 11、Python **3.11+** 和 Git。

```powershell
npm ci
npm run core:setup
npm run check
npm run core:demo
npm run core:play
```

打开 [Core 验收台](http://127.0.0.1:5184)。可从德国手牌盖放「回合末恢复」，结束各阶段，到 Draw 结束时选择使用能力及合法地区。重新运行相同命令会继续原局。如需另起一局，可指定一个新目录，例如 `npm run core:play -- --save-dir ../.cache/core-second-game`。原 Node 启动页可用 `npm run dev` 打开。

## 项目入口

| 位置 | 用途 |
| --- | --- |
| [Core 实现与验收](docs/core/implementation-v0.1.md) | 架构职责、API、测试映射、临时工程选择及未定规则 |
| [固定内容和卡牌存储](docs/core/implementation-v0.1.md#固定内容和卡牌存储) | SQLite 数据库、卡牌效果声明、构建命令及对局日志边界 |
| [本地整套转录卡库](docs/core/transcribed-content.md) | 276 个定义、380 张牌、导出/迁移/运行命令、来源与验证边界 |
| [对象池与回合末响应的测试](core/tests/test_object_lifecycle.py) | 核对无限次、一次性和限次副本的 ID、区域移动与中途重放。可运行 `npm run core:test -- tests/test_object_lifecycle.py` |
| [架构设计书原文](docs/core/architecture-baseline-v0.1.md) | 用户提供的 Architecture Baseline v0.1 |
| [AI 测试版评分与方案](docs/ai/testing-v1.md) | Core 第一版基线、评分标准、三种策略及本地可行性对照 |
| `core/qmg_core/`、`core/tests/` | 不依赖 GUI 的 Python Core 与规则/重放/信息边界测试 |
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
npm run core:test       # Python Core 机制、重放与 AI 边界
npm run core:demo       # 无 GUI 运行多步结算、保存恢复与单步 AI
npm run core:play       # 浏览器操作同一个 Core，支持中途续跑
npm run core:data -- --help   # 构建或检查固定内容数据库
npm run legacy:verify   # 核验本机 1,725 个旧文件是否仍原样保留
npm run refs:download   # 资源足够时下载/补齐锁定的 AI 源码
npm run refs:verify     # 严格核验本机参考源码；缺文件会失败
npm run check          # 类型、构建、服务检查与目录/资料边界
```

新克隆不含本机旧归档或第三方下载；`check` 会明确报告缺失并跳过其字节核验。完整本地核验使用上述两个严格命令。源码下载保留 512 MiB 可用物理内存和 30 GiB 可用磁盘保护，触线保留已有文件，可再次运行补齐。

GitHub：[MercuryNestPasUnRobot/qmg-mercury](https://github.com/MercuryNestPasUnRobot/qmg-mercury)。旧 Git 分支和标签仅留在本机，不能将 `--all`、`--mirror` 或旧归档标签推送到这个新公开仓库。
