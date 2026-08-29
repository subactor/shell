# Subactor Shell standalone installer (Windows PowerShell).
# Served at https://subactor.github.io/shell/install.ps1

$ErrorActionPreference = "Stop"

$Release = if ($env:SUBACTOR_SHELL_RELEASE) { $env:SUBACTOR_SHELL_RELEASE } else { "latest" }
$PreferGitHubIo = if ($env:SUBACTOR_SHELL_INSTALLER_USE_GITHUB_IO) { $env:SUBACTOR_SHELL_INSTALLER_USE_GITHUB_IO } else { "true" }
$SkipInit = if ($env:SUBACTOR_SHELL_SKIP_INIT) { $env:SUBACTOR_SHELL_SKIP_INIT } else { "false" }
$BinDir = if ($env:SUBACTOR_SHELL_INSTALL_DIR) { $env:SUBACTOR_SHELL_INSTALL_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }
$ShellHome = if ($env:SUBACTOR_SHELL_HOME) { $env:SUBACTOR_SHELL_HOME } else { Join-Path $env:LOCALAPPDATA "subactor-shell" }
$VenvDir = Join-Path $ShellHome "venv"
$BinPath = Join-Path $BinDir "subactor-shell.exe"
$GitHubIoBase = "https://subactor.github.io/shell"
$GitHubRepo = "subactor/shell"

function Step([string]$Message) { Write-Host "==> $Message" }

function Warn([string]$Message) { Write-Warning $Message }

function Normalize-Version([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value) -or $Value -eq "latest") { return "latest" }
  if ($Value.StartsWith("v")) { return $Value.Substring(1) }
  return $Value
}

function Get-JsonField([object]$Json, [string]$Path) {
  $current = $Json
  foreach ($part in $Path.Split(".")) {
    if ($null -eq $current) { return $null }
    $current = $current.$part
  }
  return $current
}

function Resolve-FromGitHubIo([string]$NormalizedVersion) {
  if ($NormalizedVersion -eq "latest") {
    $metadataUrl = "$GitHubIoBase/channels/latest.json"
  } else {
    $metadataUrl = "$GitHubIoBase/releases/$NormalizedVersion/release.json"
  }
  $json = Invoke-RestMethod -Uri $metadataUrl -TimeoutSec 30
  return [ordered]@{
    Version = [string]$json.version
    WheelName = [string]$json.assets.wheel.filename
    WheelUrl = [string]$json.assets.wheel.url
    WheelSha256 = [string]$json.assets.wheel.sha256
    Source = "github.io"
  }
}

function Resolve-FromGitHub([string]$NormalizedVersion) {
  if ($NormalizedVersion -eq "latest") {
    $metadataUrl = "https://api.github.com/repos/$GitHubRepo/releases/latest"
  } else {
    $metadataUrl = "https://api.github.com/repos/$GitHubRepo/releases/tags/v$NormalizedVersion"
  }
  $json = Invoke-RestMethod -Uri $metadataUrl -TimeoutSec 30
  $wheel = $json.assets | Where-Object { $_.name -like "subactor_shell-*.whl" } | Select-Object -First 1
  if (-not $wheel) { throw "No wheel asset found in GitHub release $($json.tag_name)." }
  return [ordered]@{
    Version = [string]($json.tag_name -replace '^v', '')
    WheelName = [string]$wheel.name
    WheelUrl = [string]$wheel.browser_download_url
    WheelSha256 = ""
    Source = "github"
  }
}

function Resolve-Release([string]$RequestedRelease) {
  $normalized = Normalize-Version $RequestedRelease
  $useGitHubIo = @("false", "0", "no") -notcontains ($PreferGitHubIo.ToLowerInvariant())
  if ($useGitHubIo) {
    try {
      return Resolve-FromGitHubIo $normalized
    } catch {
      Warn "Could not resolve release from $GitHubIoBase; falling back to GitHub Releases."
    }
  }
  return Resolve-FromGitHub $normalized
}

function Find-Python() {
  foreach ($candidate in @("python", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
      if ($candidate -eq "py") {
        return @("py", "-3")
      }
      return @($candidate)
    }
  }
  throw "Python 3.11+ is required. Install Python and retry."
}

function Ensure-PythonVersion([string[]]$PythonCmd) {
  $versionText = & $PythonCmd[0] $PythonCmd[1..($PythonCmd.Length-1)] -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
  $parts = $versionText.Trim().Split(".")
  $major = [int]$parts[0]
  $minor = [int]$parts[1]
  if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    throw "Python 3.11+ is required (found $versionText)."
  }
}

function Verify-Digest([string]$Path, [string]$Expected) {
  if ([string]::IsNullOrWhiteSpace($Expected)) { return }
  $hash = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($hash -ne $Expected.ToLowerInvariant()) {
    throw "Checksum mismatch for $(Split-Path -Leaf $Path)."
  }
}

function Install-Wheel([string[]]$PythonCmd, [string]$WheelPath) {
  Step "Creating isolated environment in $VenvDir"
  New-Item -ItemType Directory -Force -Path $ShellHome | Out-Null
  $venvPython = Join-Path $VenvDir "Scripts\python.exe"
  if (-not (Test-Path $venvPython)) {
    & $PythonCmd[0] $PythonCmd[1..($PythonCmd.Length-1)] -m venv $VenvDir
  }
  & $venvPython -m pip install --disable-pip-version-check -q --upgrade pip wheel
  & $venvPython -m pip install --disable-pip-version-check -q --upgrade $WheelPath
  New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
  $venvExe = Join-Path $VenvDir "Scripts\subactor-shell.exe"
  Copy-Item -Force $venvExe $BinPath
}

function Path-Hint() {
  $pathValue = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($pathValue -notlike "*$BinDir*") {
    Warn "$BinDir is not on PATH."
    Write-Host "Add it in PowerShell:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', `"$BinDir;`$env:Path`", 'User')"
  }
}

function Run-Init() {
  if ($SkipInit -eq "true") { return }
  if (Test-Path $BinPath) {
    Step "Initializing Subactor Shell"
    try { & $BinPath init } catch { Warn "subactor-shell init failed; you can rerun it manually." }
  }
}

Step "Resolving Subactor Shell release ($Release)"
$resolved = Resolve-Release $Release
Step "Resolved $($resolved.Version) from $($resolved.Source) ($($resolved.WheelName))"

$pythonCmd = Find-Python
Ensure-PythonVersion $pythonCmd

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("subactor-shell-" + [guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $tmpDir | Out-Null
$wheelPath = Join-Path $tmpDir $resolved.WheelName
try {
  Step "Downloading $($resolved.WheelUrl)"
  Invoke-WebRequest -Uri $resolved.WheelUrl -OutFile $wheelPath -TimeoutSec 300
  Verify-Digest $wheelPath $resolved.WheelSha256
  Install-Wheel $pythonCmd $wheelPath
  Path-Hint
  Run-Init
  Step "Installed Subactor Shell $($resolved.Version) to $BinPath"
  Write-Host ""
  Write-Host "Run:"
  Write-Host "  subactor-shell chat"
  Write-Host ""
} finally {
  Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
}
