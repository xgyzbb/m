# 本机定时跑站点4/5（住宅IP 绕过 Cloudflare 对数据中心IP的封锁）
# 由 Windows 计划任务每5分钟用 pwsh 静默调用；用 $PSScriptRoot 自取目录，避免中文路径字面量
$env:ONLY_SITES = "4,5"
$env:STATE_FILE = Join-Path $PSScriptRoot "state_local.json"
$env:LOG_FILE   = Join-Path $PSScriptRoot "run_local.log"
Set-Location $PSScriptRoot
$pythonw = "D:\桌面\AI\AI培训\双童实践\01-信息雷达\.venv\Scripts\pythonw.exe"
& $pythonw (Join-Path $PSScriptRoot "monitor.py")
