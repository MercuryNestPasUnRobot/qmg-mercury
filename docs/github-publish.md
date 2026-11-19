# GitHub 发布交接

目标仓库为 [MercuryNestPasUnRobot/qmg-mercury](https://github.com/MercuryNestPasUnRobot/qmg-mercury)，用户已创建公开空仓库。origin 已设置，新项目已形成独立本地提交。

首次通过 GitHub 连接器创建 README 的请求返回 `403 Resource not accessible by integration`，没有写入成功。连接器列出的可用 GitHub App 安装为空；这不等于用户账号没有仓库权限，而是当前连接不能完成该写入。本机 Git Credential Manager 也没有可用的 GitHub 凭据。

继续方法任选其一：

1. 在 [GitHub 应用安装设置](https://github.com/settings/installations) 为 Codex/ChatGPT 所用的 GitHub 应用授权此仓库；如未安装，在 Codex 的 GitHub 连接入口完成配置，然后让助手继续同步。
2. 由用户在本机完成 Git Credential Manager 登录，随后只推送当前新分支：

```powershell
git push -u origin HEAD:main
```

不要使用 `--all`、`--mirror` 或推送旧标签。当前分支的提交只含新代码、文档、哈希清单和上游来源索引；旧素材、存档、训练资料与源码快照被排除。`.githooks/pre-push` 也会拒绝包含旧 HEAD 的历史。

推送后必须核验远程树、分支 HEAD 与 GitHub Actions 状态。连接成功或设置 origin 本身不代表已经发布，也不代表远程 CI 已通过。
