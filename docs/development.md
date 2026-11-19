# 开发与版本管理

## 环境

当前已进入 Python Core 阶段：先准备 Python 3.11+ 并运行 `npm run core:setup`。`npm run check` 现在还包括 Core 的 pytest 检查；`npm run core:demo` 提供无 GUI 的结算/重放/单步模拟演示。具体接口及正式规则边界见 [Core 实现说明](core/implementation-v0.1.md)。下文 Node 启动页说明仍适用。

本机使用 Node.js 24.12.0、npm 11.6.2。新项目只安装 TypeScript 5.9.3、`@types/node` 24.10.1 和其类型依赖，不读取旧 `node_modules` 或旧 Python 环境。`.npmrc` 指向官方 npm 注册表，缓存放在新 `.cache/npm/`。

```powershell
npm ci
npm run doctor
npm run check
npm run dev
```

`dev` 使用 Node.js 原生 TypeScript 支持；`build` 用 tsc 编译到 `dist/`，`start` 运行编译版本。服务只监听 `127.0.0.1`，默认端口 5173。更换端口可在 PowerShell 运行 `$env:PORT = '5174'`。健康接口为 `/health`。启动页和健康接口以外的路径都返回 404，不对外暴露工作目录、旧存档或参考源码。

`npm run check` 运行类型检查、构建、4 项服务检查、源码/Git 目录边界、可用的本地归档和参考库校验。新克隆没有本机归档或参考下载，允许缺失的步骤会明确说明未执行字节核验；使用 `legacy:verify` 和 `refs:verify` 可执行严格本机检查。

CI 在 Windows 与 Ubuntu 执行 `npm ci` 和 `npm run check`。本地通过不等于远程 CI 已通过，以 GitHub Actions 的实际结果为准。

## Git

公开仓库：[MercuryNestPasUnRobot/qmg-mercury](https://github.com/MercuryNestPasUnRobot/qmg-mercury)。根目录新代码与文档属于新项目，旧文件由 `.gitignore` 排除，但本地仍保留旧分支、标签和 Git 对象。

后续按功能建立 `codex/` 前缀分支，每次提交前执行检查并审阅暂存内容：

本机已设置 `core.hooksPath=.githooks`；新克隆运行 `tools/setup.ps1` 或 `git config core.hooksPath .githooks` 可启用。提交前核验目录边界和本机归档；推送前拒绝旧任务分支、归档标签或包含旧 HEAD 的提交。钩子是操作保护，归档仍是普通本地目录，完整性以哈希检查为准。

```powershell
git status --short
git diff --cached --stat
npm run check
git commit -m "说明实际完成的改动"
```

不得使用 `git add -f legacy`、`git push --all`、`git push --mirror` 或推送旧归档标签。公开库只接收新代码、说明和索引；不要上传旧卡图、规则 PDF、存档、模型、训练资料、缓存和第三方源码快照。

本机已通过系统现有 Git Credential Manager 凭据成功推送。当前 GitHub 连接器的内容写入仍返回 403，后续发布使用系统 Git；不要把令牌写进远程 URL、仓库或日志。当前本地 `codex/reboot-2026-11-19` 跟踪 `origin/main`，推送当前基线使用 `git push origin HEAD:main`。后续新功能建议从新基线建立独立分支并通过 PR 合并。

## 本机归档与参考库

`npm run legacy:verify` 核验 1,725 个重要文件的 SHA-256，并检查冻结范围是否增加了文件。它不执行旧程序。清单列明不逐文件核验的依赖、缓存和临时目录；这些目录仍保留原样。

`npm run refs:download` 从固定提交下载约 5 MiB AI 代码，单文件最多 2 MiB、单项目最多 25 MiB、总计最多 50 MiB；最多两个请求同时进行。可用物理内存低于 512 MiB 或可用磁盘低于 30 GiB 时，保留已有文件并报告延后。它会核验体积和 Git blob SHA-1，另生成 SHA-256 报告 `.cache/references-report.json`。已有正确文件会复用，不删除或修补不匹配的本地文件。

下载失败或内存不足后可再次运行；不需要重新下载整个仓库。只读源码不能独立编译运行 Forge 或 TripleA；后续如果需要运行上游项目，须单独建立各自 Java/Maven/Gradle 环境。
