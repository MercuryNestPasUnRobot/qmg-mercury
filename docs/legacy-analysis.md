# 旧项目分析与资源清点

分析日期：2026-11-19。冻结目录：`legacy/2026-11-19/`。旧 HEAD：`86181b157a27fe8333fc30bb6bee032acc27653f`，原分支 `task/T03-cost-recovery-study`，原库没有远程地址。本报告是读源码、任务台账和已有证据后的分析；本轮没有重新运行旧游戏、旧测试或训练。

## 实际进度

旧产品是 Windows 桌面《战场军需官》规则预览，采用 React + TypeScript + Vite + Electron；Python 3.11 训练服务通过桥接调用 TypeScript 规则源。已有六国回合、开局弃牌、陆海空基础动作、补给与积分、响应/增强嵌套结算、内容编辑器、保存恢复、规则 AI、房主直连和回放观察室。它不是通过完整验收的正式游戏。

任务状态以冻结的 `docs/ai/tasks.md` 和各任务报告为准：

| 范围 | 旧台账状态 | 可复用成果与限制 |
| --- | --- | --- |
| T01—T11 | ACCEPTED | 基线备份、观察契约、记录/查询、真人与规则 AI 记录、历史战场和播放均有既有证据 |
| T12 | IN_PROGRESS | 席位和观察权限已有实现及回环检查；真实双电脑验收未完成 |
| T13、T14 | PLANNED | 房主录制重连、赛后导出/导入尚未实施 |
| T15 | IN_PROGRESS、未验收 | 40 局真实训练录制、候选和牌区读取、成本及回归有证据；800 局正式评测和训练接入交付未完成 |
| T16—T20 | PLANNED | 恢复、运行中查询、训练总览、一键桌面读取、最终交付尚未完成 |

旧 T15 报告记录 1,436 项测试与 5 项联机自动化通过。这是历史报告，不是新库测试结果。正式评测两个批次分别完成 98、17 局后触及 512 MiB 物理余量停止线，不能合并声称 800 局验收通过，也不能据此声称 AI 强度已达标。

旧 README 与卡牌收尾资料记录 380 个卡位、276 个不同卡面（246 张具名牌与 30 种通用牌）。代码中已有可执行效果并有逐卡测试引用；正式数量、扩展替换关系、图片文字与完整规则准确性仍需人工复核，旧测试内容包仍标记 `verified: false`。

## 代码结构与复用方式

下列路径均相对于冻结目录；引用时先复制到新目录，归档本体保持不变。

| 模块 | 阅读入口 | 复用价值 | 重建时需要解决 |
| --- | --- | --- | --- |
| 规则与状态 | `src/engine/types.ts`、`game.ts`、`effect-handlers.ts`、`resolution-suspension.ts` | 动作合法性、结算顺序、付款/取消/响应案例 | `game.ts` 承担大量职责；先定义稳定契约和小场景，再按机制迁移，不能整段接入即视为正确 |
| 内容与地图 | `src/engine/content.ts`、`reviewed-cards.ts`、`map.ts`、`assets/*.json` | 卡牌录入、地区邻接、校对索引 | 地图轮廓、海峡和规则来源仍需校对；图像授权与程序代码许可分别处理 |
| 决策与可见信息 | `src/ai/environment.ts`、`choose.ts`、`encoding.ts`、`worker.ts` | 合法候选、逐步环境、规则策略、隐藏信息视图 | 桌面 Worker 实际调用规则 AI；设置 active-model 不等于游戏加载了训练模型 |
| 记录与回放 | `src/ai/replay.ts`、`board-frame.ts`、`scripts/replay-storage.ts`、`replay-reader.ts`、`replay-repository.ts` | 真实前后帧、牌区、积分账本、已验证前缀、只读查询 | 区分动作提交与完整结算树；精确恢复尚未验收；初期以短局和小快照控制成本 |
| UI 与联机 | `src/App.tsx`、`src/replay/`、`src/network/RemoteGame.tsx`、`electron/room.cjs` | 交互原型、授权视角、断线/重连案例 | 不把回环自动化当作跨电脑验收；游戏规则不能绑定到 UI 层 |
| 训练与模型身份 | `training/qmg_training/{bridge,trainer,model,storage,identity,manager}.py` | 128 维观察、96 维动作、64 维 GRU、模仿/PPO 草稿、成果身份保护 | 信息表达和记忆覆盖不足、长训可靠恢复/模型强度/ONNX 实际游戏接入尚未闭环；不直接搬虚拟环境 |
| 验证案例 | `src/**/*.test.ts`、`training/tests/`、`docs/ai/reports/` | 对嵌套响应、空军、积分、授权、读档等问题的具体回归场景 | 按新契约改写并实际运行；旧通过数不能移作新验收 |

## 可用资料

| 资料 | 冻结位置 | 本轮处理 |
| --- | --- | --- |
| 英文基础/全面战争规则 PDF | `reference/ARTG006-QuartermasterGeneralWW2-Rulebook-EN-web.pdf`、`ARTG010-QuartermasterGeneralWW2-TotalWar-Rulebook-EN-web.pdf` | 原样保留；后续规则核验的原始资料，本轮未逐页重新解析 |
| 原图、地图与图集 | `Images/`、`public/assets/`、`assets/source-manifest.json` | 原样保留及纳入重要文件哈希；不上传公开库 |
| 卡牌 OCR、转录与校对 | `assets/card-*.json`、`legacy-card-transcription.json`、`docs/card-*.md` | 可作为人工校对候选；不等同于规则权威来源 |
| 需求和实现说明 | `docs/battlefield-replay-prd-v0.3.md`、`docs/ai/*v0.5.md`、`docs/replay*.md` | 保留需求、历史决定与未完成状态，新的计划见单独文档 |
| 训练与回放记录 | `training-data/`、`docs/ai/reports/`、`tmp/` | 保留完整现场；不重新训练、不清理失败实验、不改写旧评测结论 |
| 旧运行依赖 | `node_modules/`、`training/.venv/`、`.training-runtime/` | 原样隔离；不作为新环境的依赖来源 |

原工作目录约 8.6 GiB（不含约 192 MiB Git 对象和不可读取的生成目录）；其中大量为缓存、虚拟环境和临时实验，不适合重新装载或公开上传。重要源码/资料清单覆盖 **1,725 文件、730,004,876 字节**；逐文件 SHA-256 在 `resources/legacy/manifest.json`。缓存、依赖、临时运行及 `__pycache__` 不逐文件哈希，但全部随原目录移动保留。旧 `.training-runtime/bootstrap/` 和少量临时目录曾不可读取，未尝试修改权限或删除。

## 重建原则

优先复用已明确来源的规则案例、地图/卡牌数据结构和失败经验。先形成可确定复现的规则与授权观察契约，再搭规则 AI、最小记录与读取，最后考虑学习型 AI 和搜索。训练成本、记录成本、实际游戏接入、AI 强度分别验收，避免以其中一项替代其余项目。

旧分支与所有现有 worktree 均保留。`archive/pre-reboot-2026-11-19` 指向旧 HEAD，只涵盖该提交已有文件；原先未跟踪的 T04—T06 资料、T05/T06 实验等在本机冻结目录及哈希清单中，不冒充已经被旧提交追踪。
