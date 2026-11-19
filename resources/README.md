# 资源库索引

范围：万智牌 **Forge** 与 **TripleA** 的 AI 参考实现，以及本机被冻结的旧项目。用户已明确更正“独弈”为 Forge，因此本资源库不下载 Ludii。

## 来源和大小

| 项目 | 官方来源 | 固定提交 | 本次清单 | 用途 |
| --- | --- | --- | --- | --- |
| Forge | [Card-Forge/forge](https://github.com/Card-Forge/forge) | `9748ef2c96d18bcb55271e20e63c32bdee3e991c` | 259 文件、4,023,499 字节，约 3.84 MiB | 卡牌决策、费用、目标选择、战斗和模拟 |
| TripleA | [triplea-game/triplea](https://github.com/triplea-game/triplea) | `84ad0f7911d46d1c0fd37592606137e29e5f7ebe` | 88 文件、1,176,399 字节，约 1.12 MiB | 战略评估、进攻/非战斗移动、采购、撤退和影响图 |

上游仓库 GitHub `size` 字段分别为 1,095,163 KiB 和 727,859 KiB（含仓库历史的服务端指标，不是精确的下载压缩包大小）。本次不克隆完整仓库，只选择约 4.96 MiB AI 源码、必要模块说明和许可证。

机器索引是 [sources.lock.json](sources.lock.json)，包含每个文件的路径、字节数和 Git blob 校验值。实际下载状态以本机 `.cache/references-report.json` 或 `npm run refs:verify` 为准，清单存在不代表已全部下载。公共 GitHub 版本只保存索引，下载内容位于被忽略的 `resources/upstream/`。

## Forge 阅读路线

本地根目录为 `resources/upstream/forge/`，远程根目录为 [锁定提交](https://github.com/Card-Forge/forge/tree/9748ef2c96d18bcb55271e20e63c32bdee3e991c/forge-ai)。

| 顺序 | 文件或目录（相对本地根） | 阅读目标 |
| --- | --- | --- |
| 1 | `forge-ai/src/main/java/forge/ai/AiController.java` | 决策总入口和阶段调度 |
| 2 | `forge-ai/src/main/java/forge/ai/SpellAbilityAi.java`、`ability/` | 通用能力接口与逐能力策略拆分 |
| 3 | `forge-ai/src/main/java/forge/ai/AiCostDecision.java`、`AiCardMemory.java` | 支付选择与 AI 对已知卡牌的记忆 |
| 4 | `forge-ai/src/main/java/forge/ai/AiAttackController.java`、`AiBlockController.java` | 攻防选择与估值 |
| 5 | `forge-ai/src/main/java/forge/ai/simulation/GameSimulator.java`、`GameStateEvaluator.java`、`HiddenInfoShuffler.java`、`FlatMonteCarloPicker.java` | 状态复制、模拟估值、隐藏信息抽样与搜索候选 |

## TripleA 阅读路线

本地根目录为 `resources/upstream/triplea/`，核心策略远程入口为 [Pro AI](https://github.com/triplea-game/triplea/tree/84ad0f7911d46d1c0fd37592606137e29e5f7ebe/game-app/game-core/src/main/java/games/strategy/triplea/ai/pro)。只下载新 `game-app/ai` 会遗漏 Pro AI，因此索引同时包含旧路径下的核心策略。

| 顺序 | 文件或目录（相对本地根） | 阅读目标 |
| --- | --- | --- |
| 1 | `game-app/game-core/src/main/java/games/strategy/triplea/ai/AbstractAi.java` | 引擎与 AI 的边界 |
| 2 | 同目录 `pro/ProAi.java`、`ProCombatMoveAi.java`、`ProNonCombatMoveAi.java` | 总调度与战斗/非战斗阶段规划 |
| 3 | 同目录 `pro/ProPurchaseAi.java`、`ProRetreatAi.java`、`pro/util/` | 资源投入、撤退与战斗估值工具 |
| 4 | 同目录 `fast/`、`weak/` | 基线策略对照 |
| 5 | `game-app/ai/src/main/java/org/triplea/ai/flowfield/` | 影响图、相邻关系与战力估计 |

这些实现依赖上游各自的完整规则与对象模型，不能直接作为 QMG AI 使用。表中的借鉴用途是本项目的阅读建议，不代表已经验证移植效果。

## 旧项目导航

旧库分析见 [legacy-analysis.md](../docs/legacy-analysis.md)，机器索引见 [legacy/index.json](legacy/index.json)。所有旧路径均位于 `legacy/2026-11-19/`；保留了原有目录层次和文档链接。重要文件的字节快照见 [legacy/manifest.json](legacy/manifest.json)。

## 来源与许可

两个项目均附带 GNU GPL v3 许可证原文：[Forge LICENSE](https://github.com/Card-Forge/forge/blob/9748ef2c96d18bcb55271e20e63c32bdee3e991c/LICENSE)、[TripleA LICENSE](https://github.com/triplea-game/triplea/blob/84ad0f7911d46d1c0fd37592606137e29e5f7ebe/LICENSE)。下载保存上游原文，不修改版权头。新产品目前没有复制、链接或打包这些源码；将来复制代码时应记录具体来源并先确定项目相应的许可安排。图片、卡牌与商标不因代码公开而自动获得使用许可。
