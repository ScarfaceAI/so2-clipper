param([string]$Destination = (Join-Path $env:USERPROFILE '.agents\skills\so2-clipper'))
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$target = [IO.Path]::GetFullPath($Destination)
if ($source -eq $target) { Write-Output "Already installed: $target"; exit 0 }
if (Test-Path -LiteralPath $target) { throw "Destination exists; compare versions before updating: $target" }
if (-not (Test-Path -LiteralPath (Join-Path $source 'runtime\scripts\kill_clipper.py'))) {
    throw 'Install the complete ZIP release; the development source is not a standalone runtime.'
}
$parent = Split-Path -Parent $target
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$staging = $target + '.install-' + [guid]::NewGuid().ToString('N')
Copy-Item -LiteralPath $source -Destination $staging -Recurse
Move-Item -LiteralPath $staging -Destination $target
Write-Output "Installed: $target"
Write-Output 'Reload skills in the AI tool, then provide a local SO2 video or folder.'
