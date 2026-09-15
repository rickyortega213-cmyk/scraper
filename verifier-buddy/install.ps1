# Verifier Buddy installer for Windows. Run in PowerShell:
#
#   irm https://raw.githubusercontent.com/rickyortega213-cmyk/scraper/HEAD/verifier-buddy/install.ps1 | iex
#
# Clones the repo's default branch into %USERPROFILE%\.verifier-buddy and puts
# a `verifier` command on your PATH that pulls the latest version from GitHub
# every time it starts (set VERIFIER_NO_UPDATE=1 to skip that once).
#
# Needs git (https://git-scm.com/download/win) and Python 3.9+
# (https://www.python.org/downloads/ - tick "Add python.exe to PATH").
#
#   & "$HOME\.verifier-buddy\verifier-buddy\install.ps1" -Uninstall
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$RepoUrl = if ($env:VERIFIER_REPO) { $env:VERIFIER_REPO } else { "https://github.com/rickyortega213-cmyk/scraper.git" }
$Branch  = $env:VERIFIER_BRANCH
$RepoDir = if ($env:VERIFIER_HOME) { $env:VERIFIER_HOME } else { Join-Path $HOME ".verifier-buddy" }
$BinDir  = Join-Path $env:LOCALAPPDATA "verifier-buddy\bin"
$Launcher = Join-Path $BinDir "verifier.cmd"

if ($Uninstall) {
    if (Test-Path $Launcher) { Remove-Item $Launcher -Force; Write-Host "removed $Launcher" }
    Write-Host "clone left in place: $RepoDir   (delete it with: Remove-Item -Recurse -Force '$RepoDir')"
    Write-Host "saved API key left in: $env:APPDATA\verifier-buddy"
    exit 0
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required. Install it from https://git-scm.com/download/win and re-run."
}
$py = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    $exe = $candidate[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $ver = & $exe @($candidate[1..($candidate.Length - 1)] + @("-c", "import sys; print(sys.version_info >= (3, 9))")) 2>$null
        if ("$ver".Trim() -eq "True") { $py = ($candidate -join " "); break }
    }
}
if (-not $py) {
    throw "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and re-run."
}

if (Test-Path (Join-Path $RepoDir "verifier-buddy\verifier")) {
    Write-Host "using checkout at $RepoDir"
    if (-not $Branch) {
        $head = (& git ls-remote --symref $RepoUrl HEAD 2>$null | Select-String "^ref: refs/heads/(\S+)")
        if ($head) { $Branch = $head.Matches[0].Groups[1].Value }
    }
    $current = (& git -C $RepoDir rev-parse --abbrev-ref HEAD 2>$null)
    if ($Branch -and $current -ne $Branch) {
        Write-Host "switching from $current to $Branch"
        & git -C $RepoDir fetch --quiet origin $Branch
        & git -C $RepoDir checkout --quiet -B $Branch FETCH_HEAD
        & git -C $RepoDir config remote.origin.fetch "+refs/heads/${Branch}:refs/remotes/origin/${Branch}"
        & git -C $RepoDir branch --quiet --set-upstream-to "origin/$Branch" 2>$null
    }
    & git -C $RepoDir pull --ff-only --quiet 2>$null
} else {
    if (Test-Path $RepoDir) { throw "$RepoDir exists but is not a Verifier Buddy checkout; move it or set VERIFIER_HOME" }
    if ($Branch) {
        Write-Host "cloning $RepoUrl ($Branch) -> $RepoDir"
        & git clone --quiet --branch $Branch --single-branch $RepoUrl $RepoDir
    } else {
        Write-Host "cloning $RepoUrl (default branch) -> $RepoDir"
        & git clone --quiet --single-branch $RepoUrl $RepoDir
    }
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$cmd = @"
@echo off
rem Verifier Buddy launcher (written by install.ps1). GitHub is the source of
rem truth: each run pulls the latest commit before starting.
setlocal
set "REPO_DIR=$RepoDir"
set "SCRIPT=%REPO_DIR%\verifier-buddy\verifier"
if not exist "%SCRIPT%" (
  echo verifier: %SCRIPT% is missing. Re-install with:
  echo   irm https://raw.githubusercontent.com/rickyortega213-cmyk/scraper/HEAD/verifier-buddy/install.ps1 ^| iex
  exit /b 1
)
if not "%VERIFIER_NO_UPDATE%"=="1" (
  for /f "delims=" %%r in ('git -C "%REPO_DIR%" rev-parse --short HEAD 2^>nul') do set "BEFORE=%%r"
  set GIT_TERMINAL_PROMPT=0
  git -C "%REPO_DIR%" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=8 pull --ff-only --quiet >nul 2>&1
  if errorlevel 1 echo   (could not update from GitHub; running the local version)
  for /f "delims=" %%r in ('git -C "%REPO_DIR%" rev-parse --short HEAD 2^>nul') do set "VERIFIER_REV=%%r"
  if not "%BEFORE%"=="%VERIFIER_REV%" set "VERIFIER_UPDATED=%BEFORE% -> %VERIFIER_REV%"
)
$py "%SCRIPT%" %*
"@
Set-Content -Path $Launcher -Value $cmd -Encoding ASCII
Write-Host "installed launcher -> $Launcher  (source: $RepoDir)"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($userPath -split ";") -contains $BinDir)) {
    [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(";")) + ";" + $BinDir), "User")
    $env:Path += ";" + $BinDir
    Write-Host "added $BinDir to your PATH"
}

Write-Host ""
Write-Host "done - open a new PowerShell or Command Prompt window and type:  verifier"
Write-Host "(it checks GitHub for a newer version every time it starts)"
