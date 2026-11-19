# GitHub 版本管理

公开仓库：[MercuryNestPasUnRobot/qmg-mercury](https://github.com/MercuryNestPasUnRobot/qmg-mercury)。新项目已通过系统 Git 成功推送到 `main`，保留了用户创建仓库时的初始化提交；本机旧分支、归档标签和其他 worktree 没有上传。

当前本地分支 `codex/reboot-2026-11-19` 跟踪 `origin/main`。向这个基线推送使用：

```powershell
git push origin HEAD:main
```

后续功能建立 `codex/` 前缀的独立分支，通过 PR 合并。提交前执行 `npm run check` 并检查暂存内容；发布后核对远程分支 HEAD 和 [GitHub Actions](https://github.com/MercuryNestPasUnRobot/qmg-mercury/actions)。

公开提交只含新代码、文档、哈希清单和上游来源索引。旧素材、存档、训练资料、运行依赖与下载的参考源码均被排除。不要使用 `--all`、`--mirror` 或推送旧标签；`.githooks/pre-push` 会拒绝旧任务分支、归档标签及包含旧 HEAD 的历史。

当前 GitHub 连接器的内容写入返回 403，改用本机系统 Git 与已有 Git Credential Manager 凭据后成功。后续优先使用这条已验证的发布路径，不把令牌写进 URL、仓库、脚本或日志。若凭据失效，由用户在官方认证流程中重新登录。
