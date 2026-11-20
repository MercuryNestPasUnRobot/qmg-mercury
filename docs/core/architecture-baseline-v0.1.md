# QMG 数字化复原项目
## 第一轮底层 Core 架构设计书

**版本：** Architecture Baseline v0.1  
**日期：** 2026 年 11 月 19 日  
**规则基准：** Quartermaster General: WW2 Second Edition（基础版）  
**设计范围：** 图形界面 — 数据与日志 — 核心规则引擎 — AI 评价接口  
**项目定位：** 可复原、可测试、可扩展、可供 AI 模拟的数字桌游引擎

---

# 第一章：整体项目目标与可实现功能

## 1.1 项目总体目标

项目旨在数字化复原《战场军需官：二战》第二版，建立一套完整、统一、可验证的规则引擎，并以此为基础开发具有战略目的的电脑 AI。

项目分为三个主要目标：

1. **规则复原：** 实现地图、国家、单位、卡牌、补给、回合、积分以及卡牌组合效果，确保玩家操作与规则一致。
2. **人机统一：** 图形界面与 AI 使用同一套合法性判断和规则执行接口，不允许两套独立规则。
3. **战略决策：** AI 以最大化阵营胜利为最终目标，使用确定性 VP 等效评分和单次行动模拟评估局面。

项目采取“先正确、后完整、再智能”的开发策略。

## 1.2 核心设计原则

以下原则作为第一轮正式架构约束。

**原则 A：位置化动态状态**

所有真实卡牌、单位及隐藏能力副本使用统一的 Token 体系。

Token 的动态状态主要由它所在的位置（Position / Zone）表示。

例如：

- 陆军位于德国地区，表示该陆军正在德国。
- 陆军位于预备区，表示尚未部署。
- 卡牌位于手牌区，表示可以按规则从手中打出。
- 卡牌位于状态区，表示它是已部署状态牌。
- 隐藏能力令牌位于冷却区，表示对应能力暂时不可再次使用。

不为单张卡牌或单位设置额外的 `is_used`、`is_supplied`、`is_active` 等可从位置或规则推导的动态字段。

**原则 B：操作统一化**

所有游戏对象的物理变化由统一的 Position Transfer（位置转移）机制执行。

但是一次具有规则意义的操作不一定产生位置变化，因此还需保留操作类型、目标、来源与执行结果。

例如：攻击空白地区不会移除单位，却仍然是一次有效战斗。

**原则 C：严格串行结算**

每次只执行一个当前原子结算步骤。

如果需要玩家做选择，必须等待选择完成，才能继续结算。

卡牌效果使用后进先出（LIFO）结算栈处理嵌套效果；同一张卡中按文字先后执行的效果，则按相反顺序压栈以保证执行顺序正确。

**原则 D：结算后统一监听**

每次具有规则意义的原子步骤完成后，全量扫描符合监听资格的能力。

监听器可以：

- 自动生成进一步的结算指令；
- 提出玩家可选择使用的能力；
- 改变结算步骤后续的合法性；
- 产生嵌套操作。

对于预防性效果，先完成“宣告操作”的结算检查点，再决定是否提交实际状态变化。

**原则 E：完整记录与可重放**

每次有效操作写入回合内日志，按回合或指定检查点保存完整状态快照。

日志记录操作的规则语义以及状态变化，支持复盘、故障定位、AI 测试和确定性重放。

**原则 F：统一 VP 评分**

全部 AI 局势评价使用等效 VP 作为共同标尺。

真实 VP 与估计的战略 VP 分开记录。

第一版不引入胜率预测模型，不进行跨回合的递归博弈搜索。

## 1.3 功能范围与实施优先级

| 等级 | 功能 | 目标 |
|---|---|---|
| P0 | 地图、国家、单位、卡牌及区域数据 | 建立完整状态容器 |
| P0 | GUI 地图、手牌、对象位置展示 | 支持基础操作 |
| P0 | 合法性检查与统一操作执行器 | 保证操作正确 |
| P0 | 结算栈及玩家选择 | 支持多步骤行动 |
| P0 | 操作日志、快照与重放 | 支持调试与恢复 |
| P1 | 六国完整基础回合与积分 | 支持基础游戏 |
| P1 | 卡牌组合、状态能力、全量触发扫描 | 支持复杂效果 |
| P1 | 真实卡牌规则数据导入 | 扩充可玩内容 |
| P2 | 确定性 AI 权重评分与单步模拟 | 实现电脑玩家 |
| P2 | AI 批量自我对战与参数校准 | 改善策略 |
| P3 | 联机、完整响应优先权、复杂扩展规则 | 后续扩展 |

P0 完成的标志不是所有卡牌都已录入，而是**从选牌到规则执行、状态更新、日志记录、界面刷新的一条完整运行链已经可用**。

## 1.4 首轮暂不实现的内容

- 完整的多人联网、游戏大厅和匹配服务。
- AI 神经网络训练和深度博弈搜索。
- 《Total War》等扩展内容。
- 全部卡牌的自动导入与 OCR。
- 复杂的动画、音效和商业化界面。
- 无法在第一批测试中覆盖的完整响应优先权规则。

但引擎的接口应允许这些功能在未来接入，而不需要重写基础状态模型。

---

# 第二章：项目细分架构

## 2.1 整体分层

项目采用六个逻辑层次。

| 层次 | 名称 | 核心职责 |
|---|---|---|
| L1 | Presentation Layer | GUI、地图、卡牌显示和输入 |
| L2 | Application/API Layer | 统一操作入口、玩家视图、合法选择 |
| L3 | Core Execution Layer | 回合、合法性、卡牌解释、结算与触发 |
| L4 | State & Data Layer | Token 位置、地图、分数、规则数据 |
| L5 | Persistence & Diagnostics | 回合日志、存档、快照、重放 |
| L6 | AI & Simulation Layer | 候选行动、局面模拟、评分、决策 |

L6 不是 L3 的上级。AI 只能通过统一 API 操作 Core，不能直接改变 GameState。

日志与保存系统同样不得绕过 Core 进行游戏规则修改。

## 2.2 图形界面系统

暂定为 Web GUI，Core 独立运行。

第一版 GUI 包括：

**MapView：** 显示全部地区、连接关系、单位位置、得分点和合法目标高亮。

**HandPanel：** 显示当前玩家可见的手牌及选中卡牌。

**ZonePanel：** 显示牌库数量、弃牌区、状态区、响应区和单位预备区。

**ResolutionPanel：** 显示当前正在结算的卡牌、当前操作与可选目标。

**ScorePanel：** 显示两个阵营的真实 VP、当前回合与阶段。

**HistoryPanel：** 显示允许公开的操作历史。

GUI 的职责被限制为三个步骤：

1. 展示经过权限过滤的游戏状态；
2. 将玩家选择转换成标准 Command；
3. 接收并显示 Core 的执行结果。

GUI 不自行判断补给、攻击合法性或积分规则。

## 2.3 Core 规则系统

第一轮 Core 正式模块名单如下。

| 模块 | 建议类名 | 职责 |
|---|---|---|
| 状态容器 | `GameState` | 保存全部权威运行状态 |
| 地图模块 | `MapGraph` | 地区、连接、补给相关查询 |
| 区域管理 | `ZoneRegistry` | 统一管理 Token 位置及区域顺序 |
| 回合控制 | `TurnController` | 国家顺序、阶段、回合推进 |
| 指令入口 | `CommandProcessor` | 接收并检查玩家指令 |
| 合法性判断 | `LegalActionGenerator` | 生成合法选择并最终验证 |
| 卡牌解释 | `CardInterpreter` | 将卡牌效果编译为基础操作 |
| 结算执行 | `ResolutionEngine` | 严格串行执行结算栈 |
| 操作执行 | `OperationExecutor` | 原子化提交位置变化及其他规则变更 |
| 触发扫描 | `AbilityScanner` | 全量检查可用能力及触发条件 |
| 规则修饰 | `RuleModifierResolver` | 读取持续状态牌产生的规则变化 |
| 补给计算 | `SupplyResolver` | 计算陆海军补给关系 |
| 积分计算 | `ScoreResolver` | VP 结算及胜负判断 |
| 日志记录 | `OperationLogger` | 记录每次有效操作 |
| 保存与重放 | `SaveRepository` | 管理快照、版本、恢复与日志重放 |

注意：这是**职责名单，不要求每个模块都立即成为独立进程或大型文件**。

第一版可以在一个 Python 项目中采用轻量模块化设计。

## 2.4 数据系统分类

建议将数据分为四种。

**静态定义数据**

- MapDefinition
- RegionDefinition
- ConnectionDefinition
- CardDefinition
- AbilityDefinition
- CountryDefinition
- ScoringRule
- RuleSet

**动态游戏数据**

- Token 的位置映射
- 各 Zone 内的对象顺序
- 两个阵营的真实分数
- 当前轮次、国家和阶段
- 当前结算栈
- 当前等待的玩家选择

**执行与记录数据**

- Operation
- OperationOutcome
- OperationRecord
- ResolutionFrame
- GameSnapshot

**AI 评价数据**

- EvaluationWeights
- TokenInfluence
- ScorePointValue
- CardPotential
- PositionValue

后两类中的评价结果默认临时计算，不写入 Token 实例。

## 2.5 技术栈建议

第一轮采用：

- **Python：** 规则 Core、AI、模拟与测试。
- **JSON：** 地图、卡牌定义、权重与初始化配置。
- **SQLite 或 JSONL：** 日志及快照持久化；原型可先用 JSONL。
- **Web UI：** 地图、卡牌及交互界面。
- **pytest：** 规则测试、属性测试和重放测试。
- **Git / GitHub：** 代码版本管理与架构文档版本化。

所有规则必须能够在不启动 GUI 的情况下运行。

---

# 第三章：各对象的定义

## 3.1 对象设计总原则

第一版对象分为：

1. **实体对象：** 游戏中实际存在的卡牌、单位及隐藏令牌。
2. **位置对象：** 实体可以位于的地区与区域。
3. **规则定义对象：** 描述静态规则和可执行操作。
4. **结算对象：** 描述当前正在处理的工作。
5. **状态与记录对象：** 保存游戏事实和操作历史。
6. **评价对象：** 为 AI 提供临时分析结果。

不把每个对象都设计为复杂继承类。

优先使用轻量数据类、统一 ID 和静态定义引用。

## 3.2 基础实体：Token

`Token` 是全部位置化游戏实体的基础对象。

核心字段：

| 字段 | 说明 |
|---|---|
| `token_id` | 唯一实例 ID |
| `definition_id` | 对应静态定义 |
| `token_type` | UNIT / CARD / ABILITY / SCORE_SOURCE 等 |
| `owner_country` | 归属国家；允许为空 |
| `position_id` | 当前所在地区或区域 |
| `source_token_id` | 可选，隐藏副本对应的来源 Token |

`position_id` 是 Token 的主要动态属性。

不保存独立的补给、已使用、待触发等标志。

**UnitToken**

表示陆军和海军。

单位类型读取 UnitDefinition。

预备区与棋盘地区都是合法 Position。

**CardToken**

表示真实卡牌实例。

通过 `definition_id` 对应具体 CardDefinition。

允许在牌库、手牌、弃牌区、状态区、响应区和结算区之间转移。

**AbilityToken**

表示状态牌产生的隐藏能力副本。

通过不可变的来源关系绑定对应 CardToken。

通过可用区、待结算区、冷却区和失效区表达能力生命周期。

**ScoreSourceToken**

可选对象，用来表示卡牌创建、激活或移除的动态得分来源。

静态地图得分点不需要创建 Token。

## 3.3 位置与地图对象

**Position**

统一的位置引用，既可指向地图地区，也可指向抽象 Zone。

**Zone**

卡牌、单位和隐藏能力的容器。

推荐的区域：

| Zone 类型 | 用途 |
|---|---|
| `DECK` | 国家牌库 |
| `HAND` | 国家手牌 |
| `DISCARD` | 国家弃牌区 |
| `STATUS` | 已部署状态牌 |
| `RESPONSE` | 已部署响应牌 |
| `RESERVE_ARMY` | 陆军预备区 |
| `RESERVE_NAVY` | 海军预备区 |
| `RESOLUTION` | 正在结算的真实卡牌 |
| `ABILITY_READY` | 可监听的隐藏能力副本 |
| `ABILITY_PENDING` | 已触发、等待解决的能力 |
| `ABILITY_COOLDOWN` | 已使用、等待复位的能力 |
| `ABILITY_INACTIVE` | 已失效能力 |
| `SCORE_DORMANT` | 尚未激活的额外得分来源 |

每个国家可以拥有独立的牌库、手牌和弃牌区。

有序区域必须保存 Token 排列顺序，尤其是牌库与弃牌堆。

**Region**

静态地图地区对象。

字段包括：

- region_id
- name
- region_type：LAND / SEA
- base_score_source
- home_country（若适用）
- base_supply_source（若适用）

**Connection**

地图连接对象。

字段包括：

- connection_id
- region_a
- region_b
- connection_type
- movement_or_supply_tags

静态地图连接不需要动态状态。

**MapGraph**

保存 Region 与 Connection，提供相邻查询、路径查询、补给网络构建及有效建设位置判断所需的地图基础数据。

由卡牌产生或取消的额外连接，可在后续通过放置于相应位置的 ModifierToken 形成有效连接图。

**ScoreSiteDefinition**

定义得分点的静态属性：

- 对应 Region
- 基础计分规则
- 每次计分的基准 VP
- 允许哪些国家获得分数
- 是否需要额外激活条件

不得将“当前是否由某方控制”写成静态字段；必须从当前 Token 位置计算。

## 3.4 卡牌与规则对象

**CardDefinition**

保存真实卡牌的静态规则：

- card_id
- country
- card_type
- effect_sequence
- ability_definitions
- requirements
- printed_reference
- ruleset_version

**AbilityDefinition**

描述隐藏能力副本的行为：

- ability_id
- trigger_condition
- effect_sequence
- activation_cost
- activation_mode：AUTO / OPTIONAL
- reset_policy
- listener_scope
- priority_rule

**RuleModifierDefinition**

描述持续修饰规则，例如：

- 改变某类建设的允许地点；
- 改变某种操作的费用；
- 改变计分规则；
- 改变补给关系。

这类效果在合法性计算或积分计算时读取，不依赖操作完成后的监听扫描。

## 3.5 操作对象

**Command**

代表 GUI 或 AI 提交的玩家意图。

例如选择某张卡、选择攻击目标、选择是否使用状态能力。

Command 不直接修改 GameState。

**Operation**

代表 Core 中准备执行的一项有规则语义的操作。

标准字段：

- operation_id
- operation_type
- actor_country
- source_token_id
- target_position_id
- target_token_id
- parameters
- parent_resolution_id

Operation 的典型类型：

- TRANSFER
- BUILD_ARMY
- BUILD_NAVY
- RECRUIT
- LAND_BATTLE
- SEA_BATTLE
- ELIMINATE
- DRAW
- DISCARD
- GAIN_VP
- ANNOUNCE
- BEGIN_PHASE
- END_PHASE

其中 BUILD、RECRUIT、BATTLE 和 ELIMINATE 的规则语义不同，不能全部退化成无区别的 TRANSFER。

**OperationOutcome**

操作执行结果，包括：

- 是否成功；
- 实际位置变化列表；
- 分数变化；
- 已完成的规则事件类型；
- 规则上下文；
- 是否需要进一步操作。

OperationOutcome 在执行过程中生成，可直接转化成日志记录。

**OperationRecord**

持久化操作记录。

至少包含：

- sequence_id
- game_id
- round_id
- turn_id
- resolution_id
- operation_type
- source
- actor
- targets
- changes
- result
- ruleset_version

记录支持零位置变化的有效操作，例如攻击空白地区。

## 3.6 结算对象

**ResolutionFrame**

代表一次正在执行的卡牌或能力效果序列。

保存：

- resolution_id
- source_card_id
- current_step
- remaining_operations
- selected_targets
- eligible_token_ids
- processed_token_ids
- parent_resolution_id

这是结算器的临时执行数据，不是添加在卡牌或单位上的动态状态。

用于铁路运输类连续操作时，已处理单位 ID 可保存在当前 ResolutionFrame 中，避免给每支陆军增加使用标志。

**ResolutionStack**

采用 LIFO 结构。

负责：

- 保存待执行指令；
- 保证嵌套结算先执行；
- 暂停原操作并处理能力触发；
- 恢复被暂停的效果序列。

**DecisionRequest**

统一玩家选择请求。

字段包括：

- decision_id
- required_country
- decision_type
- legal_options
- originating_operation_id
- optional_flag

只允许一个当前未完成的阻塞式玩家选择。

没有收到合法选择之前，不推进普通结算。

**AbilityScanner**

不是游戏实体，而是规则服务。

在每个规定的结算检查点扫描可监听的 AbilityToken，使用刚刚完成的 OperationOutcome 与 GameState 判断是否满足触发条件。

同一操作只进入一次对应监听检查过程。

## 3.7 全局状态对象

**GameState**

整个游戏唯一权威状态。

包含：

- Token 位置映射；
- Zone 内顺序；
- 当前回合和阶段；
- 两阵营实际 VP；
- 当前结算栈；
- 当前 DecisionRequest；
- 当前状态版本；
- 终局结果。

**TurnClock**

保存轮次、当前国家和当前阶段。

国家与真人玩家分离，允许一个真人控制多个国家。

**TeamScore**

保存轴心与同盟实际 VP，使用整数即可。

不必为了统一位置模型而强行用 Token 实现整数运算。图形界面可以将 TeamScore 表现为计分轨道标记。

**GameSnapshot**

某个状态版本的完整序列化结果。

**Observation / KnowledgeView**

为特定国家或玩家提供允许观察到的局面。

不得暴露对手隐藏手牌、隐藏响应牌或未来牌库顺序。

## 3.8 AI 评价对象

第一轮确定以下评价结果类型：

| 名称 | 作用 |
|---|---|
| `TokenInfluence` | 计算单位对目标地区与周边兵力的影响 |
| `ForceInfluence` | 汇总区域友军支持和敌方压力 |
| `SupplyNetworkValue` | 计算补给与关键连接的边际价值 |
| `ScorePointValue` | 将得分点控制与争夺潜力换算为 VP |
| `CardIntrinsicValue` | 卡牌在初始化时计算的固有价值 |
| `CardPotential` | 卡牌在当前局面的未实现价值 |
| `ResourceValue` | 牌库、手牌与预备单位的资源修正 |
| `PositionValue` | 当前阵营的最终等效 VP 分值 |
| `EvaluationWeights` | 全部可调评分权重配置 |

以上全部可以是临时计算对象，不需要为 Token 增加状态字段。

---

# 第四章：工作流与架构交互

## 4.1 游戏初始化

标准初始化过程：

1. 加载规则版本和静态卡牌定义。
2. 加载地图地区与全部连接。
3. 加载固定得分点和潜在动态得分来源定义。
4. 初始化国家、阵营、单位数量和对应 Zone。
5. 生成 CardToken、UnitToken 以及需要预创建的隐藏令牌。
6. 根据规则部署初始陆军。
7. 洗牌、抽取起始手牌并完成初始化弃牌。
8. 设置起始回合、国家、阶段及双方 VP。
9. 保存初始 GameSnapshot 和随机种子或洗牌结果记录。
10. 通知 GUI 获取第一份 Observation。

基础版完整回合顺序为：

德国 → 英国 → 日本 → 苏联 → 意大利 → 美国。

单国回合阶段为：

Play → Supply → Victory → Discard → Draw。

## 4.2 标准玩家操作

一次正常的卡牌操作流程：

1. GUI 获取合法行动与目标。
2. 玩家选中卡牌并提交 Command。
3. CommandProcessor 验证指令来源及当前阶段。
4. 合法性判断器验证当前游戏状态。
5. 将卡牌移动到公用结算区。
6. CardInterpreter 读取 CardDefinition。
7. 生成 ResolutionFrame，按照正确执行顺序将操作压栈。
8. ResolutionEngine 依次执行原子操作。
9. 每次有效操作提交后更新 GameState。
10. 生成 OperationRecord。
11. AbilityScanner 扫描符合资格的能力。
12. 处理需要立即执行的自动能力及玩家选择。
13. 完成所有嵌套结算后继续原始卡牌。
14. 按卡牌规则将卡牌移至弃牌区或部署区。
15. 更新日志和 GUI。

所有状态改变都必须经由统一的 OperationExecutor 或其受控的状态提交机制。

## 4.3 原子结算模型

结算器的主要指令分为两类：

**位置转移指令：** 执行确定的、已通过合法性验证的 Token 位置变化。

**选择指令：** 向玩家或 AI 请求一次合法选择，再根据选择生成后续操作。

此外，需要少量系统指令处理积分、宣告、阶段推进、记录等没有实体移动的规则效果。

引擎遵循：

执行原子步骤 → 提交有效结果 → 写日志 → 扫描触发 → 处理匹配效果 → 继续原栈。

需要注意：

- 玩家选择必须完成后才能执行依赖它的动作。
- 并非每次内部数据移动都要开启新一轮监听，例如将隐藏令牌移入冷却区属于当前能力结算的内部变更。
- 监听检查点以**完成一个具有规则意义的操作**为边界。
- 监听必须使用当前操作的语义，而不是只比较前后 Token 位置。

## 4.4 阻止类效果

部分能力会阻止尚未发生的变化。

处理方式：

1. 完成 ANNOUNCE 操作，声明即将发生的行为和目标。
2. 执行该宣告检查点的监听。
3. 处理可能阻止、替换或改变操作的能力。
4. 根据结果决定是否提交原操作。
5. 完成后继续结算。

这样仍然采用统一结算循环，而不是另建独立的异步响应系统。

完整阵营响应优先权可在后续版本扩展。

## 4.5 有使用次数限制的能力

以 Blitzkrieg 类“每回合一次”的状态能力为例：

1. 真实状态牌位于 STATUS。
2. 对应 AbilityToken 位于 ABILITY_READY。
3. 发生符合条件的陆战操作。
4. 扫描器识别符合条件的 AbilityToken。
5. 如果使用是可选的，生成 DecisionRequest。
6. 玩家选择使用后，按规则支付费用。
7. 将 AbilityToken 移至 ABILITY_COOLDOWN。
8. 将效果推入 ResolutionStack。
9. 效果完成后恢复原结算。
10. 到达定义好的复位边界时，将能力副本送回 ABILITY_READY。

对于每次触发一次的能力，应以当前触发检查过程作为复位边界，而不是一律在回合结束复位。

原始状态牌被移除后，其关联能力副本必须同步失效。

## 4.6 连续部署或重新建设

以铁路运输类效果作为测试：

1. 卡牌开始结算时读取当前合格单位集合。
2. 玩家选择其中一支尚未处理的单位。
3. 从当前地图位置暂时移除该单位。
4. 在临时结算状态中重新计算合法建设位置。
5. 玩家选择目标，执行重新建设。
6. 更新棋盘并记录 OperationRecord。
7. 根据当前规则扫描可能触发的效果。
8. 返回原卡牌继续处理剩余合格单位。
9. 全部完成或合法终止时结束结算。

应确保失败的重新建设不会使原单位意外丢失。

每一步重新计算补给与目标合法性，不能使用整张牌开始时缓存的所有建设位置。

## 4.7 非卡牌阶段

Core 需要允许系统在没有 CardToken 进入结算区的情况下启动规则操作。

例如：

**Supply Step：** 计算当前国家所有单位的补给情况，按照规则移除失去补给的单位。

**Victory Step：** 计算当前国家所控制的得分点，以及已部署状态牌提供的积分。

**Discard Step：** 允许玩家选择符合规则的弃牌操作。

**Draw Step：** 从该国家牌库抽牌直至规定手牌数量，处理牌库耗尽等特殊情况。

**Round End：** 更新轮次，检查完整回合结束时的胜利条件。

以上规则操作同样经过日志和规定的触发检查点。

## 4.8 日志、保存与恢复

日志采用追加记录方式。

每次有效操作保存：

- 执行前状态版本；
- 操作语义；
- 玩家选择；
- 来源和目标；
- 状态变化；
- 执行后状态版本。

完整回合结束时生成快照。

如需中途存档，必须同时保存当前结算栈与未完成的玩家选择。

日志与状态修改应保证原子一致性：不能出现棋盘已经变化但对应的成功操作记录丢失，或日志声称成功但实际没有完成。

恢复流程：

读取最近有效快照 → 按顺序重放后续记录 → 校验状态一致性 → 恢复游戏。

## 4.9 核心架构测试

第一轮至少包含：

- 位置不变量：每个 Token 恰好处于一个合法位置。
- 地区占据约束：禁止非法的敌对单位共处。
- 牌区数量与顺序正确性。
- 建设和征募合法性差异。
- 战斗与直接消灭语义差异。
- 空白地区战斗依然生成有效操作记录。
- 连续建设后合法位置重新计算。
- 玩家未选择时不能推进结算。
- 同一使用次数限制能力不能在冷却期间再次发动。
- 隐藏能力副本正确失效和复位。
- 不同效果按规定顺序结算。
- 日志重放得到相同的 GameState。
- AI 与 GUI 提交相同 Command 得到相同规则结果。
- 观察视图不泄露对手手牌或牌库顺序。

上述测试全部通过，才进入大规模卡牌录入。

---

# 第五章：AI 构建架构

## 5.1 AI 的最终目标

AI 的唯一最终战略目标是尽可能获得胜利。

QMG 中，阵营主要通过地图得分点和卡牌效果获得 VP。

因此，全部中间战略评价统一换算成**等效 VP**。

真实累计 VP 不等于预测的未来 VP；后者仅是确定性战略评价值。

## 5.2 第一版决策方式

AI 使用单步候选模拟：

1. 获取当前可观察状态。
2. 获取全部合法行动。
3. 对每个候选行动构建模拟状态。
4. 完成该行动涉及的当前卡牌结算。
5. 重新计算局势价值。
6. 计算行动前后的价值变化。
7. 选择评价最高的行动。
8. 通过统一 Command 接口提交。

不向后递归模拟下一回合的行动。

对于一张卡内部的多个选择，允许在同一张卡牌的结算范围内逐步优化或枚举有限的完整选择组合，但不延伸成跨回合搜索。

## 5.3 总体评分规则

定义：

**V(S) = ΔVP + MapValue + CardValue + ResourceAdjustment**

其中：

- ΔVP：己方实际 VP 减敌方实际 VP。
- MapValue：地图上未来可取得或保护的 VP 等效价值差。
- CardValue：尚未转化为实际分数或地图优势的卡牌价值差。
- ResourceAdjustment：资源短缺、牌库耗尽等尚未计入的额外影响。

行动价值：

**Q(a) = V(S_after) − V(S_before)**

实际终局胜负优先于任何启发式加分。

## 5.4 TokenInfluence 计算

每个单位为附近地区计算两组影响：

**Score Influence**

代表单位与附近得分点之间的战略关联。

参考因素：

- 有效行动距离；
- 兵力类型；
- 补给可达性；
- 单位预备资源；
- 可用建设手段；
- 当前得分点的战略价值。

**Military Influence**

代表附近友军与敌军之间的相互影响。

参考因素：

- 附近友军数量；
- 附近敌军数量；
- 地图连接关系；
- 进攻和防守可操作性；
- 关键补给连接。

建议基础公式：

**I(u,p) = Wtype × Wdistance × Wsupply × Wresource**

这些系数全部是权重，而非概率。

建议的距离权重初值：

| 有效行动距离 | 权重 |
|---|---|
| 0 | 1.00 |
| 1 | 0.65 |
| 2 | 0.30 |
| 3 | 0.10 |
| 不可达 | 0.00 |

参数仅作为初始化参考，后续通过测试局面校准。

已占据得分点的单位不应因预备兵力为零就失去自身的驻守价值；资源权重主要用于扩张能力判断。

## 5.5 区域竞争与得分点评价

对每个得分点 p：

**Fp = 己方全部相关 Token 的影响力总和**

**Ep = 敌方全部相关 Token 的影响力总和**

定义有界竞争优势：

**Ap = clip((Fp − Ep) / K, −1, 1)**

K 为权重饱和参数。

Ap 是竞争优势指标，不是占领概率。

然后为得分点计算：

**Vp = Bp × (Cp + αXp − βTp)**

其中：

- Bp：得分点在有限规划窗口内可产生的基准 VP。
- Cp：实际控制带来的持续得分系数。
- Xp：扩张潜力。
- Tp：受威胁程度。
- α、β：可调战略权重。

Xp 和 Tp 由竞争优势、当前占领情况及补给条件确定，并限制在合理范围内。

每个得分点只汇总一次，防止多个 Token 对同一得分来源重复计分。

## 5.6 时间权重

未来得分使用确定性时间折扣：

**Bp = rp × Σ γ^(i−1)**

求和覆盖规划窗口内的有效计分机会。

- rp：该得分点每次实际计分可提供的 VP。
- γ：未来收益折扣权重。
- H：规划计分窗口。

第一版建议 H 为未来 2～4 次相关国家的计分机会，γ 可从 0.8 附近开始调试。

时间折扣不是概率，只用于表达当前 AI 对近期收益的偏好。

## 5.7 补给网络价值

使用两种方法：

**全局方法：** 计算地图当前的有效补给网络，并将其可达性直接纳入 ScorePointValue。

**边际方法：** 为单个单位计算移除它前后的地图价值变化：

**N(u) = MapValue(S) − MapValue(S without u)**

N(u) 可用于：

- 判断应当保护哪支单位；
- 判断应当攻击哪个敌军；
- 识别关键海军及补给节点；
- 解释 AI 为什么选择某个行动。

但不直接把所有 N(u) 加入总分，防止与地图评价重复计分。

## 5.8 卡牌两层评价

**第一层：CardIntrinsicValue**

初始化时根据卡牌静态效果生成原始价值。

参考因素包括：

- 可实现的基础行动类型；
- 理想地图收益；
- 理想资源交换价值；
- 未来持续收益；
- 额外行动机会；
- 典型组合效果。

初始价值记录于独立权重配置，不直接改变真实 CardDefinition。

**第二层：CardPotential**

根据当前局面修正：

- 当前与未来有限窗口的可用性；
- 是否存在有价值的合法目标；
- 当前阵营需要什么资源；
- 牌库及预备单位是否足够；
- 是否与已部署状态牌形成组合；
- 是否存在额外费用。

可采用：

**CardPotential(c,S) = IntrinsicValue(c) × Usability(c,S) × Demand(c,S)**

其中 Usability 和 Demand 都是确定性的权重。

尚未使用的卡牌价值计入 CardValue。

已经实现为棋盘优势的卡牌收益计入 MapValue。

已经获得的 VP 计入 ΔVP。

同一收益只能进入一个评价项。

## 5.9 状态与隐藏能力评价

对 AbilityToken 根据位置计算能力可用性：

| 位置 | 评价逻辑 |
|---|---|
| READY | 根据当前可触发性评估能力潜力 |
| PENDING | 根据当前结算的实际上下文计算 |
| COOLDOWN | 不提供当前可使用价值，但可考虑未来复位 |
| INACTIVE | 不提供能力价值 |

状态牌与其隐藏副本属于同一能力资源，不应对二者重复加分。

## 5.10 隐藏信息处理

AI 不得查看未公开的对手手牌、隐藏响应内容和牌库未来顺序。

静态卡牌定义库可以包含全部规则，但不能把某张尚未公开卡牌实际位于哪里当作 AI 已知信息。

不采用概率模型时，可以使用：

- 已公开卡牌的确定信息；
- 当前牌区数量；
- 已知卡组构成；
- 固定的未知牌资源基准权重。

如果模拟行动涉及抽取未知牌，不得利用真实牌库顺序偷看结果来评价候选行动。

可以采用隐藏抽牌的抽象资源增量，在实际提交行动后再揭示真实抽牌结果。

## 5.11 参数管理与 AI 测试

所有权重集中管理于：

`evaluation_weights.json`

第一版至少包括：

- distance_weights
- supply_weights
- military_weights
- expansion_weights
- threat_weights
- time_discount
- card_value_weights
- resource_penalties
- terminal_priority

AI 每次决策输出可解释评分：

- 候选行动；
- 实际 VP 变化；
- 地图价值变化；
- 卡牌价值变化；
- 资源修正；
- 最终 Q(a)。

通过固定测试局面检查：

- 是否优先获取有价值的得分点；
- 是否保护关键补给线；
- 是否避免没有意义的集中兵力；
- 是否正确评估用牌成本；
- 是否在临近终局时优先近期真实收益；
- 是否选择已确定能够获胜的行动。

后续通过批量自我对战校准权重，但不要求 AI 决策过程使用胜率预测。

---

# 第六章：可参考外部源

## 6.1 TripleA

**项目：** TripleA  
**仓库：** https://github.com/triplea-game/triplea  
**架构文档：** https://github.com/triplea-game/triplea/blob/main/AGENTS.md  
**历史引擎文档：** https://github.com/triplea-game/triplea/blob/main/docs/development/engine-code-overview.md  
**许可证：** GPL-3.0

重点研究：

- GameData 如何组织地图、单位和玩家；
- Delegate 如何按回合阶段组织规则；
- ChangeFactory 如何统一状态变化；
- GamePlayer 如何让 GUI 和 AI 使用同一套操作模型；
- headed/headless 如何分离图形界面和规则执行。

**借鉴范围：** 架构、地图图结构、状态修改流程、GUI 与 AI 接口分离。

**不建议直接移植：** 大型战争战斗计算器、生产系统、Java Swing GUI、完整网络联机模块。

历史引擎设计文档存在过时风险，应以当前代码和仓库说明为准。

## 6.2 Forge（独弈）

**项目：** Card-Forge / Forge  
**仓库：** https://github.com/Card-Forge/forge  
**项目 Wiki：** https://github.com/Card-Forge/forge/wiki  
**卡牌脚本 API：** https://github.com/Card-Forge/forge/wiki/Card-scripting-API  
**AI 文档：** https://github.com/Card-Forge/forge/blob/master/docs/AI.md  
**许可证：** GPL-3.0

重点研究：

- forge-game 中的卡牌区域、规则能力与触发机制；
- forge-ai 中按卡牌能力和游戏阶段组织 AI 决策的方法；
- forge-gui 与规则数据的交互方式；
- 卡牌脚本与核心规则代码的边界；
- 多步骤卡牌结算、玩家选择和自动化测试。

**借鉴范围：** CardDefinition、AbilityDefinition、触发、区域管理、结算接口、卡牌脚本。

**不建议直接移植：** 万智牌完整优先权系统、费用系统、海量特殊规则与游戏专属对象体系。

Forge 的复杂度远超过基础版 QMG，应按具体模块提取设计思想。

## 6.3 boardgame.io

**项目：** boardgame.io  
**仓库：** https://github.com/boardgameio/boardgame.io  
**文档：** https://boardgame.io/documentation  
**许可证：** MIT

重点研究：

- 游戏状态与回合上下文分离；
- Move、Phase、Stage；
- 操作日志与可重放状态；
- 玩家可见状态过滤；
- GUI 与底层游戏状态的分离。

这是一个适合参考轻量回合框架的项目。

如采用 Python Core，则主要借鉴其接口模式，不必引入完整 JavaScript 引擎。

## 6.4 QMG 官方规则

**第二版发行方：** Ares Games  
**产品与规则入口：** https://www.aresgames.eu/games/euro-games/ww2-quartermaster-general  
**第二版规则在线参考：** https://www.rulespal.com/quartermaster-general-ww2-2nd-edition/rulebook

规则资料主要用于：

- 核对国家、单位和卡牌数量；
- 核对地图、补给和得分规则；
- 核对行动先后顺序；
- 核对状态牌与响应牌的触发条件；
- 为每张卡建立可验证的测试案例。

需要特别注意版本差异：第一版、第二版与扩展版的同名卡牌，效果不一定相同。

## 6.5 外部代码与素材使用原则

外部代码应先作为只读参考资源保存，不直接复制进入 QMG Core。

如果后续需要整合 GPL 项目源码，必须另行核对许可证义务及分发方式。

QMG 原版卡牌文本、卡面美术和地图素材，应与开源程序代码分开处理。公开发布需要另行确认相关权利和授权条件。

对每份外部资源记录：

- 项目名称；
- 仓库地址；
- 精确 commit；
- 许可证；
- 所参考的模块；
- 对应的内部设计或实现文件。

## 6.6 第一轮编码前仍需确认的事项

**规则层面**

- 所有第二版卡牌的完整清单、文本及版本；
- 铁路运输相关卡牌的准确名称与效果；
- 各状态能力的使用次数与复位边界；
- 同一触发条件下多个能力的优先顺序；
- 可以改变得分点或地图连接关系的真实卡牌清单。

**架构层面**

- 卡牌效果定义采用 JSON 指令还是部分 Python 特殊处理器；
- 动态 ScoreSourceToken 是否属于首轮 MVP；
- 日志持久化选择 JSONL 还是 SQLite；
- GUI 的最终技术栈；
- 多重合法选择的候选枚举上限。

**AI 层面**

- 各类卡牌 IntrinsicValue 的初始化权重；
- Score Influence 与 Military Influence 的具体聚合函数；
- 首轮权重参数表；
- 隐藏抽牌的抽象资源价值；
- 战略评价窗口的默认长度。

以上事项尚未定稿，不应由 Coding Agent 擅自设定成正式游戏规则。

---

## 第一轮架构验收标准

完成第一轮时，必须达到以下结果：

1. GameState 可以统一管理全部卡牌、单位与隐藏令牌的位置。
2. 地图与区域数据能够独立加载、查询和验证。
3. GUI 与 AI 均通过统一合法性接口提交操作。
4. 每个具有规则意义的操作均由 Core 正确结算，并产生可追踪日志。
5. 一张卡牌可以包含多个按顺序执行的操作。
6. 严格串行结算能够阻塞等待玩家选择。
7. 自动与可选状态能力能够通过全量扫描触发。
8. 隐藏能力副本可以通过区域转移完成冷却与复位。
9. 每次操作的历史可以重放，恢复到一致状态。
10. ScoreEvaluator 可以使用确定性权重输出可解释的等效 VP。
11. AI 可以完成一次合法候选行动的模拟与评分。
12. 测试框架能够自动检查规则不变量和核心边界案例。

**第一轮架构完成的核心判断：任何新卡牌都应优先通过组合已有的 Operation、AbilityDefinition 和 ResolutionFrame 实现；只有基础规则语义确实缺失时才扩展 Core。**

这将作为后续 Codex 任务拆分、代码审核、模块验收和性能优化的统一设计标准。