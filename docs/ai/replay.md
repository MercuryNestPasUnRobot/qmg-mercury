# Mercury 录像台

双击仓库中的 `local-content/replays/打开录像.html`。在顶部选择测试批次，再点击想看的对局。浏览器可以离线打开，录像文件和转录牌文保存在本机。

- 用播放、暂停、前后单步、进度条或回合下拉框定位。左右方向键可单步，空格可以播放或暂停。
- 地图用坦克、舰船和飞机棋子分别显示陆军、海军和空军。单位按所属国家着色，颜色图例在棋盘下方。同一步的出兵、移除或调动会标亮相关地区。点击地区可看驻军、基础连接和据点价值，两阵营累计分会随进度更新。
- 六席牌区展示状态牌、停用牌、响应数和手牌数。开启裁判视角可查看盖牌、监听对象、AI 候选评分和回退原因。牌区的卡牌可点击查看牌文。
- 每一步对应一条 AI 指令。旁边的结算列表按 Core 顺序列出同一步内发生的地图、场上牌区和分值变化。

## 保存新批次

用同一个六席入口运行。命令会创建独立日志目录，自动核对已完成对局并更新录像库。

```powershell
npm run core:ai-match -- --catalog ../local-content/baselines/core-v1-preview/content.sqlite3 --seeds 31 73 --name "响应选择对照" --ai-version "AI 试验版 v1" --ai-note "B 限时结算，A 规则回退"
```

每个批次保留名称、AI 版本与简介、日期、种子、阵营分配和终局分数。批次数据还记录预算、候选宽度、分支上限及权重指纹。逐步评分源日志写入 `local-content/ai-tests/`，规则卡库副本按指纹单独保存在 `local-content/replays/rules/`。

## 从日志复现

读取器使用原始卡库与种子初始化 Core，通过逐席合法命令接口执行日志里的已选指令。它核对决策索引、版本、席位、终局分数与胜负，新批次同时核对终局记录链。核对成功的局才会出现在录像库。复现发生在对局完成后，且独立于 AI 决策计时。裁判视角是回看时的显示选项，录像仍应留在私有目录。

```powershell
npm run core:replay -- --batch ../local-content/ai-tests/six-seat-v1 --catalog ../local-content/baselines/core-v1-preview/content.sqlite3 --name "六席 AI 对照 · 第一批" --ai-version "AI 试验版 v1" --ai-note "B 限时结算，A 规则回退。三个相同种子互换阵营。"
```

已归档批次可省略 `--catalog`，读取器会按摘要的指纹选取已保存的卡库。旧批次可能需要其原版 Core 才能重建。已经生成的录像页面可以继续离线打开。

## 桌游棋盘

录像库使用本机 `local-content/replays/board/map.png`。底图取自冻结旧项目的 `public/assets/map.png`，尺寸 3366 × 1803，SHA-256 为 `5b39607005da9deffb4f2d30165c29758755d87a4669bff2cce039a91ab54a03`。50 个棋子锚点来自同一旧项目的 `src/engine/map.ts`，仅用于显示。规则邻接仍来自录像本身。底图保留在本机，缺少底图的环境会显示示意地图。

界面更新后可在仓库根目录执行以下命令，一次更新所有已有录像的外观，保留已核对的帧和对局日志。

```powershell
npm run core:replay -- --refresh
```
