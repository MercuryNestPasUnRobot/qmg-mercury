# 本地转录卡库接入

旧档中的 276 个卡牌定义、380 张实体牌已接入新 Core，覆盖六个行动席位及八个部队归属。原卡文、效果声明、备注和旧审核来源完整保存在本地内容包，不上传公开仓库。旧档保持冻结；新运行时读取独立 JSON 或 SQLite，不导入旧引擎。

## 生成与运行

在仓库根目录运行：

```powershell
npm run core:export-legacy
npm run core:migrate -- ../local-content/legacy-2026-11-19/content.json ../local-content/legacy-2026-11-19/core
npm run core:verify-content -- ../local-content/legacy-2026-11-19/core/rules.json --output ../local-content/legacy-2026-11-19/core/smoke-report.json
$catalog = (Get-Content local-content/legacy-2026-11-19/core/coverage.json -Raw | ConvertFrom-Json).catalog
npm run core:play -- --catalog $catalog --save-dir ../.cache/transcribed-match
```

验收台目前固定由人类操作德国，其余席位可由 AI 按同一个 CoreAPI 代办。它用于验证 Core 接口和续跑，不是完整的多人游戏界面。AI 的评分权重仍是未经训练和标定的工程样例，新增空军权重也属于该样例。

导出工具先逐字节复制七个允许的数据源，再在新目录中导出其声明；不会执行旧游戏引擎。已存在但与冻结来源不同的源文件副本会导致导出失败。`provenance.json` 记录每个来源的路径、字节数和 SHA-256。复制来源包括旧 `src/engine/{types,map,reviewed-cards,content}.ts`、通用牌清单、原始转录和卡牌工作清单。机制解释参考旧 `src/engine/game.ts`，由新 Python 解释器实现。

## 数据与记录边界

| 本地路径 | 内容 |
| --- | --- |
| `local-content/legacy-2026-11-19/source/` | 原样数据源副本，作为后续核对依据 |
| `local-content/legacy-2026-11-19/content.json` | 全部旧转录、卡组、地图及部队上限的结构化导出 |
| `local-content/legacy-2026-11-19/provenance.json` | 来源哈希 |
| `local-content/legacy-2026-11-19/core/rules.json` | 新 Core 的完整静态定义及原文元数据 |
| `local-content/legacy-2026-11-19/core/<digest>.sqlite3` | 按内容哈希命名的不可变固定数据数据库 |
| `local-content/legacy-2026-11-19/core/coverage.json` | 每张牌的转换状态、缺失项、数据库路径和指纹 |
| `local-content/legacy-2026-11-19/core/smoke-report.json` | 每张牌的开局检查结果和检查范围 |
| `.cache/transcribed-match/` | 对局快照与追加式 JSONL 操作日志 |

卡牌的 `source_card` 保留完整原始声明，`title`、`text` 保留原名称和卡文。实际执行使用经过验证的效果指令、监听器和规则修正，不执行卡文中的字符串或代码。未知指令或未知限定字段不会悄悄忽略：转换报告将其列为阻塞，运行时禁止使用未补齐的牌。

`local-content/` 已被 Git 忽略，目录边界检查还会拒绝将其加入索引。公共代码包含导出、迁移和解释器以及原创测试场景，不包含整套转录。数据库不能充当对局日志目录；日志只通过规则指纹关联固定数据。

## 已接通的执行机制

- 建设、征募、陆战、海战、部队选择与连续战斗；战斗选择具体受害实体，回合初相邻关系及实体 ID 保留到本回合结束。
- 经济战的发动和目标响应、数量修正、按单位计数、空军距离加成、取消、牌库耗尽扣分、由目标国家选择弃牌或移除部队。同一经济牌对同一目标的加成仅应用一次。
- 严格手牌代价先选齐再支付；指定其他席位选择、分支、可选步骤、卡牌检索、洗牌、牌库顶排序、弃牌回收、展示弃牌和额外出牌。多用途牌在额外出牌时仍需选择实际操作。
- 状态与暗置响应、手牌中的增援监听、主动状态、即时响应、状态发动前拦截、停用、弃置来源以及后续目标重验。被动规则修正在结算前也产生状态宣告窗口。
- 空军部署、调动、制空权、空军行动后监听；战斗中的防御牺牲与进攻方反击；失去地面/海军基地后的相邻迁移或移除。被阻止的牺牲不会视为代价已支付。
- 八个部队归属各自的上限、空军预备区、附属部队控制、海军陆军基地约束、共享补给线、海峡控制、改设本土及地图连接修正。

保护、临时补给、停用状态恢复、空军回合机会和资源调配机会都由预分配的对象表示。对象从池进入响应区，监听回合末后进入结算区，再按自身结算声明移动来源对象或机会对象。不存在额外的 Token 使用次数、保护布尔值、空军本回合已用标记或统一恢复函数；实体总数和 ID 保持封闭。状态修正与补给查询按现有对象位置推导。

## 验证范围

`npm run core:test` 覆盖新增机制的有序结算、来源移除、代价被阻止、对方席位选择、临时效果过期、对象守恒、中途保存恢复和隐藏信息投影。本地有转录导出时，还核验全部定义、380 张牌、原文逐字相等、八个归属和 SQLite 往返一致性；新克隆没有本地内容时明确跳过该项。

`core:verify-content` 对每个定义检查开局可用操作、完成选择后的结算终止和实体守恒，另检查全部手牌监听器是否注册。没有开局目标和只检查注册的监听器会单独列出，不能把这项检查说成每张牌所有触发场景均已验收。

逐牌比对及六席出牌验收可从项目根目录运行。命令读取迁移生成的 SQLite，报告留在本地内容目录。

```powershell
$catalog = (Get-Content local-content/legacy-2026-11-19/core/coverage.json -Raw | ConvertFrom-Json).catalog
npm run core:audit-content -- ../local-content/legacy-2026-11-19/content.json $catalog --output ../local-content/legacy-2026-11-19/core/card-audit.json --runtime
```

逐牌报告比对实际传给结算器的指令和顺序，并记录嵌套分支、条件、监听、主动效果、被动规则和牌组顺序。`--runtime` 另外发牌并推进六个席位，追踪合法开局命令执行过的步骤，记录遇到的规则阻断，并为阶段监听准备从上一阶段进入的用例。对初始棋盘上无目标、未触发或仅注册的卡牌，仍需对应局面才能验证其完整行为。

2026-11-20 本地验证：`npm run check` 通过 134 项 Core 测试和 4 项 Node 测试，1,725 个旧文件及 347 个上游参考文件完整性通过。整套开局检查为 186 个操作完成、50 个手牌监听器注册、40 个定义无开局目标，无运行异常；所有定义均已转换，380 个卡牌实例和原文往返校验通过。

迁移报告当前无未实现指令，内容包保留旧 `verified` 与来源信息。规则参数沿用旧实现，开局 12 张、弃 5 张、手牌上限 7、20 轮、分差 30，牌库耗尽扣 1 分。常规转录响应使用同一优先级，响应顺序与每个时机的使用限制详见[响应时机验收](response-windows.md)。
### 棋盘连接校对

按本机 50 区棋盘的实际边界与版图两侧印刷的跨图标注复核了基础连接。不列颠只接北海，日耳曼的海岸接波罗的海。北海直接接北非，北海与地中海之间的通行取决于北非控制。地形边界只在一点相接时没有基础连接。[二版规则书（第 6–7 页）](https://c.tabletopia.com/games/quartermaster-general-ww2/rules/artg006-quartermastergeneralww2-rulebook-en-web/en#page=6)

正常迁移命令会生成独立的 `board-reviewed` 规则版本，50 区共有 122 条基础连接。本机校对版的数据库路径记录在 `local-content/baselines/board-reviewed/coverage.json`。既有录像依靠原规则指纹继续重放。录像台可切换校对后的棋盘和当局规则。
