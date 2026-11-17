param(
    [string]$Python,
    [string]$ExistingPython,
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'SO2KillClips'),
    [ValidateSet('auto','cpu','cuda')][string]$Acceleration = 'auto',
    [string]$FFmpegDir,
    [string]$ModelDir,
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
if ($ExistingPython) { $Python = $ExistingPython }
if (-not $Python) {
    foreach ($tag in @('-3.13','-3.12')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $candidate = & py $tag -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) { $Python = $candidate.Trim(); break }
        }
    }
}
if (-not $Python) {
    if ($Offline) { throw 'Offline: Python 3.12/3.13 x64 is required.' }
    $installer = Join-Path $RuntimeRoot 'python-3.13.15-amd64.exe'
    $expected = 'edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403'
    if (-not (Test-Path -LiteralPath $installer)) {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe' -OutFile $installer
    }
    if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw 'Python installer SHA256 mismatch; preserved for inspection.'
    }
    $pythonRoot = Join-Path $RuntimeRoot 'python313'
    $installArgs = @('/quiet','InstallAllUsers=0','AssociateFiles=0','PrependPath=0','Include_launcher=0',('TargetDir="' + $pythonRoot + '"'))
    $process = Start-Process -FilePath $installer -ArgumentList $installArgs -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0,3010)) { throw "Python installer failed: $($process.ExitCode)" }
    $Python = Join-Path $pythonRoot 'python.exe'
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python unavailable: $Python" }
$options = @('--root',$RuntimeRoot,'--acceleration',$Acceleration)
if ($ExistingPython) { $options += '--reuse-python' }
if ($FFmpegDir) { $options += @('--ffmpeg-dir',$FFmpegDir) }
if ($ModelDir) { $options += @('--model-dir',$ModelDir) }
if ($Offline) { $options += '--offline' }
& $Python (Join-Path $PSScriptRoot 'setup_runtime.py') @options
exit $LASTEXITCODE
