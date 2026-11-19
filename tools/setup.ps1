$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
node tools/doctor.mjs
if ($LASTEXITCODE -ne 0) { throw '需要 Node.js 24.12+（24.x）和 Git。' }
git config core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { throw 'Git 本地保护钩子配置失败。' }
npm ci
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络。' }
npm run core:setup
if ($LASTEXITCODE -ne 0) { throw 'Python Core 环境安装失败，需要 Python 3.11+。' }
npm run check
if ($LASTEXITCODE -ne 0) { throw '环境验证失败，请检查输出。' }
Write-Host '环境就绪。运行 npm run dev 打开本地启动页。'
