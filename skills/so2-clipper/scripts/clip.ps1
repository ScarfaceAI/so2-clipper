param([switch]$Offline, [Parameter(ValueFromRemainingArguments=$true)][string[]]$TaskArguments)
$ErrorActionPreference = 'Stop'
$runtimeFile = $env:SO2_CLIPS_RUNTIME
if (-not $runtimeFile) { $runtimeFile = Join-Path $env:LOCALAPPDATA 'SO2KillClips\runtime.json' }
if (-not (Test-Path -LiteralPath $runtimeFile -PathType Leaf)) {
    if ($Offline) { throw "Offline: runtime configuration missing: $runtimeFile" }
    & (Join-Path $PSScriptRoot 'setup.ps1') -RuntimeRoot (Split-Path -Parent $runtimeFile)
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $runtime.python -PathType Leaf)) { throw 'Configured Python is unavailable; rerun setup.ps1.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PATH = $runtime.ffmpeg_dir + [IO.Path]::PathSeparator + $env:PATH
$env:SO2_CLIPS_RUNTIME = $runtimeFile
if ($runtime.model_directory) { $env:SO2_CLIPS_MODEL_DIR = $runtime.model_directory }
$previousOffline = $env:SO2_CLIPS_OFFLINE
try {
    if ($Offline) { $env:SO2_CLIPS_OFFLINE = '1' }
    & $runtime.python (Join-Path $PSScriptRoot 'task.py') @TaskArguments
    $taskExit = $LASTEXITCODE
} finally {
    $env:SO2_CLIPS_OFFLINE = $previousOffline
}
exit $taskExit
