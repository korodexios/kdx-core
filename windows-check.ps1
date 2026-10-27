#requires -Version 5.1
<#
.SYNOPSIS
    korodexios-core manual Windows test harness.

.DESCRIPTION
    Runs the full check bar (ruff + mypy --strict + pytest with the coverage
    floor) and a Windows runtime smoke test:
      - kdx --version / doctor / modules list / config show / contracts check
      - kdx run --once (one clean boot and stop)
      - daemon boot with control-plane reads (GET /health, GET /api/v1/status)
      - kdx status (CLI over the discovery file, exercises the win32 pid_alive path)
      - kdx --remote heartbeat status (RPC delegation to the live daemon)
      - single-instance lock: a second daemon must refuse with exit 1
      - POST /api/v1/shutdown, then the daemon process must exit 0

    Everything (step verdicts, a machine-readable JSON summary, and the full
    command output) is written to var\windows-report.md. Send that file back
    so the results can be read.

.PARAMETER Port
    Control-plane port for the daemon smoke test (default 8199).

.PARAMETER ReportPath
    Where to write the report (default: var\windows-report.md in the repo root).

.PARAMETER IncludeSeed
    Also scaffold a throwaway project with 'kdx init' (informational).

.PARAMETER SkipRuntime
    Run only the static bar (toolchain + lint + typecheck + tests).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\windows-check.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8199,
    [string]$ReportPath = "",
    [switch]$IncludeSeed,
    [switch]$SkipRuntime
)

$ErrorActionPreference = "Stop"
$IsWin = ($env:OS -eq "Windows_NT")

$script:RepoRoot = ""
$script:ReportLines = New-Object System.Collections.Generic.List[string]
$script:StepStatus = [ordered]@{}
$script:Failures = New-Object System.Collections.Generic.List[string]
$script:UseUv = $false
$script:PyRun = $null
$script:KdxRun = $null
$script:RuffCmd = $null
$script:MypyCmd = $null
$script:PyTestCmd = $null

function Add-Report([string]$line) {
    $script:ReportLines.Add($line)
}

function Write-Step([string]$text, [string]$color = "Gray") {
    Write-Host $text -ForegroundColor $color
    Add-Report $text
}

function Find-RepoRoot {
    # $PSScriptRoot is empty when the script is started by double-click or a bare
    # path (only '& script' and dot-sourcing set it), so the script's own directory
    # is only a hint; the search walks up from each candidate until it lands on the
    # pyproject.toml that actually names this package.
    $starts = @()
    if (Test-Path (Join-Path (Get-Location).Path "pyproject.toml")) { $starts += (Get-Location).Path }
    if ($PSScriptRoot) { $starts += $PSScriptRoot }
    if ($MyInvocation.ScriptName) { $starts += (Split-Path -Parent $MyInvocation.ScriptName) }
    foreach ($root in $starts) {
        if (-not $root) { continue }
        $dir = (Resolve-Path $root -ErrorAction SilentlyContinue)
        while ($dir) {
            if (Test-Path (Join-Path $dir.Path "pyproject.toml")) { return $dir.Path }
            $dir = $dir.Parent
        }
    }
    throw "pyproject.toml not found: run this from the korodexios-core repository root."
}

function Get-TailLines {
    param([string]$Path, [int]$N = 60)
    if (-not (Test-Path $Path)) { return @("(no file)") }
    $all = Get-Content -Path $Path -ErrorAction SilentlyContinue
    if ($null -eq $all) { return @("(empty)") }
    return @($all | Select-Object -Last $N)
}

function Invoke-Cmd {
    param(
        [string[]]$Command,
        [string]$Label,
        [int[]]$Accept = @(0),
        [switch]$Info
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $outLines = New-Object System.Collections.Generic.List[string]
    $exe = $Command[0]
    $rest = $null
    if ($Command.Count -gt 1) { $rest = $Command[1..($Command.Count - 1)] }
    $code = -1
    $prevEap = $ErrorActionPreference
    # A native (non-PowerShell) command writing to stdout under $ErrorActionPreference
    # 'Stop' makes PowerShell raise a NativeCommandError, which the catch below would
    # swallow and turn into a bogus exit 1. Drop to 'Continue' for the duration of the
    # call so native output streams normally and $LASTEXITCODE stays authoritative.
    $ErrorActionPreference = "Continue"
    try {
        if ($null -eq $rest) {
            $out = & $exe 2>&1
        } else {
            $out = & $exe @rest 2>&1
        }
        $code = $LASTEXITCODE
    } catch {
        $out = @($_.Exception.Message)
        $code = 1
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $sw.Stop()
    if ($null -ne $out) {
        foreach ($line in $out) { $outLines.Add([string]$line) }
    }
    $ok = $false
    foreach ($c in $Accept) { if ($code -eq $c) { $ok = $true; break } }
    $status = "fail"
    $tag = "[FAIL]"
    $color = "Red"
    if ($Info.IsPresent) { $status = "info"; $tag = "[INFO]"; $color = "Yellow" }
    elseif ($ok) { $status = "pass"; $tag = "[PASS]"; $color = "Green" }
    else { $script:Failures.Add($Label) }
    $script:StepStatus[$Label] = $status
    $secs = "{0:0.0}" -f $sw.Elapsed.TotalSeconds
    Write-Step ("{0} {1} (exit {2}, {3}s)" -f $tag, $Label, $code, $secs) $color
    Add-Report ""
    Add-Report ("$ " + ($Command -join " "))
    foreach ($l in $outLines) { Add-Report $l }
    Add-Report ""
    return [pscustomobject]@{ ExitCode = $code; Ok = $ok; Output = ($outLines -join [char]10) }
}

function Find-StandalonePython {
    # Return a python that is NOT itself a virtualenv's python. uv 0.13 refuses
    # to build a venv from a venv's interpreter (bare exit 1, no message), so a
    # 'python' on PATH that points at another tool's venv (e.g. an agent's) must
    # not be handed to 'uv venv --python'.
    #
    # Prefer a uv-managed base interpreter (AppData\Roaming\uv\python\...). uv owns
    # these, they are guaranteed to be standalone base installs (never a venv),
    # and uv is happiest seeding from one of its own. This makes the seed robust
    # against whatever 'python' on PATH happens to resolve to.
    # uv-managed bases only exist on Windows (%APPDATA%\uv\python) and use a
    # python.exe layout; both are Windows-specific, so the whole block is
    # skipped on POSIX hosts (where $env:APPDATA is null anyway).
    $uvPyDir = $null
    if ($IsWin -and $env:APPDATA) { $uvPyDir = Join-Path $env:APPDATA "uv\python" }
    if ($uvPyDir -and (Test-Path $uvPyDir)) {
        $best = $null
        foreach ($sub in (Get-ChildItem -Path $uvPyDir -Directory -ErrorAction SilentlyContinue)) {
            $exe = Join-Path $sub.FullName "python.exe"
            if (-not (Test-Path $exe)) { continue }
            $name = $sub.Name
            if ($name -match 'cpython-3\.(\d+)') {
                $minor = [int]$Matches[1]
                # 3.13 is the documented version; take the highest 3.13.x, else the
                # highest available 3.x, so the seed tracks the newest base uv has.
                if (-not $best -or ($minor -gt $best.minor) -or (($minor -eq $best.minor) -and ($name -gt $best.name))) {
                    $best = [pscustomobject]@{ name = $name; minor = $minor; exe = $exe }
                }
            }
        }
        if ($best) { return $best.exe }
    }
    # No uv-managed base: walk PATH for a non-venv python.
    $candidates = @()
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd) { $candidates += $pyCmd.Source }
    $py3Cmd = Get-Command python3 -ErrorAction SilentlyContinue
    if ($py3Cmd) { $candidates += $py3Cmd.Source }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) {
            $parent = Split-Path -Parent $c
            if ($parent -and (Test-Path (Join-Path $parent "pyvenv.cfg"))) { continue } # it is a venv's python
            return $c
        }
    }
    # No standalone python on PATH: fall back to the Windows launcher, which always
    # resolves a base install (never a venv).
    $pyL = Get-Command py -ErrorAction SilentlyContinue
    if ($pyL) { return $pyL.Source }
    return $null
}

function Set-Up-Toolchain {
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCmd) {
        $script:UseUv = $true
        $script:PyRun = @("uv", "run", "python")
        $script:KdxRun = @("uv", "run", "kdx")
        $script:RuffCmd = @("uv", "run", "ruff")
        $script:MypyCmd = @("uv", "run", "mypy")
        $script:PyTestCmd = @("uv", "run", "python", "-m", "pytest")
        Add-Report "toolchain mode: uv"
        # Pin a STANDALONE interpreter explicitly. uv 0.13 dies at exit 1 right
        # after 'Using CPython ...' both when its discovery misses the host python
        # and when it is handed another tool's venv python (the common Windows case:
        # an agent's venv shadows 'python' on PATH). Find-StandalonePython skips
        # venv pythons and prefers the 'py' launcher, which always resolves a base
        # install; record which one was pinned.
        $pyExe = Find-StandalonePython
        $venvArgs = @("venv")
        if ($pyExe) {
            $venvArgs += @("--python", $pyExe)
            Add-Report ("pinning python: {0}" -f $pyExe)
        } else {
            Add-Report "no standalone python found; letting uv discover"
        }
        # UV_VENV_CLEAR=1 makes 'uv venv' replace an existing venv without an
        # interactive prompt; if the build does not honour it, fall back to
        # '--clear'; if both fail, surface the captured output so the cause is
        # visible without hunting the report.
        $oldClear = $env:UV_VENV_CLEAR
        $env:UV_VENV_CLEAR = "1"
        try {
            $venv = Invoke-Cmd -Command (@("uv") + $venvArgs) -Label "toolchain_uv_venv"
            if (-not $venv.Ok) {
                $venv = Invoke-Cmd -Command (@("uv") + $venvArgs + @("--clear")) -Label "toolchain_uv_venv_clear"
            }
        } finally {
            if ($null -eq $oldClear) { Remove-Item Env:\UV_VENV_CLEAR -ErrorAction SilentlyContinue }
            else { $env:UV_VENV_CLEAR = $oldClear }
        }
        if ($venv.Ok) {
            $sync = Invoke-Cmd -Command @("uv", "sync", "--extra", "dev") -Label "toolchain_uv_sync_dev"
            if (-not $sync.Ok) { throw "uv sync --extra dev failed; see the report for output." }
            return
        }
        # uv could not seed the venv even with a pinned standalone python (its
        # 0.13 seeding is brittle on some hosts). Surface what it said, then fall
        # through to the pip toolchain below, which is the documented bootstrap and
        # does not ask uv to build the venv at all.
        Write-Host "[!] uv venv output (last attempt):" -ForegroundColor Yellow
        foreach ($l in ($venv.Output -split "`n")) { Write-Host ("    " + $l) }
        Write-Host ("[!] pinned python: {0}" -f $(if ($pyExe) { $pyExe } else { "(none found)" })) -ForegroundColor Yellow
        Add-Report "uv venv failed; falling back to the pip toolchain (python -m venv + pip install -e .[dev])."
        $script:UseUv = $false
    }
    if (-not $script:UseUv) {
        # pip toolchain. Prefer a standalone python (Find-StandalonePython skips
        # venv pythons); on Windows the 'py' launcher is the reliable base install.
        $script:UseUv = $false
        $pyLauncher = $null
        if ($IsWin) {
            if (Get-Command py -ErrorAction SilentlyContinue) { $pyLauncher = "py" }
            elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyLauncher = "python" }
        } else {
            if (Get-Command python3 -ErrorAction SilentlyContinue) { $pyLauncher = "python3" }
            elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyLauncher = "python" }
        }
        if (-not $pyLauncher) { throw "Neither uv nor a python launcher was found on PATH." }
        Add-Report ("toolchain mode: {0}" -f $pyLauncher)
        $venvDir = Join-Path $script:RepoRoot ".venv"
        $venvPy = Join-Path $venvDir "Scripts\python.exe"
        if (-not $IsWin) { $venvPy = Join-Path $venvDir "bin/python" }
        # A .venv left behind by a failed uv seed is broken, and 'python -m venv'
        # into an existing directory keeps the old interpreter: remove it so the
        # launcher always builds a fresh venv from a base install.
        if (Test-Path $venvDir) {
            Add-Report ("removing existing venv before rebuild: {0}" -f $venvDir)
            Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
        }
        $mk = $null
        if ($pyLauncher -eq "py") {
            # -3.13 is the documented version; when the host has no 3.13 the py
            # launcher errors, so a plain -3 (the default base install) is the
            # fallback that actually builds the venv.
            $mk = Invoke-Cmd -Command @("py", "-3.13", "-m", "venv", $venvDir) -Label "toolchain_venv_py313"
            if (-not $mk.Ok -or -not (Test-Path $venvPy)) {
                Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
                $mk = Invoke-Cmd -Command @("py", "-3", "-m", "venv", $venvDir) -Label "toolchain_venv_py3"
            }
        } else {
            $mk = Invoke-Cmd -Command @($pyLauncher, "-m", "venv", $venvDir) -Label "toolchain_venv"
        }
        # Some Linux distros (Debian) ship python without ensurepip, so a plain
        # '-m venv' dies. Build without pip and bootstrap it from get-pip.py.
        $needGetPip = $false
        if (-not $mk.Ok -or -not (Test-Path $venvPy)) {
            if ($IsWin) {
                throw ("venv creation failed (mode {0}, exit {1}); see the report for output." -f $pyLauncher, $mk.ExitCode)
            }
            Add-Report "venv without pip (ensurepip missing); bootstrapping via get-pip.py"
            Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
            $mk = Invoke-Cmd -Command @($pyLauncher, "-m", "venv", "--without-pip", $venvDir) -Label "toolchain_venv_nopip"
            if (-not $mk.Ok -or -not (Test-Path $venvPy)) {
                throw ("venv creation failed (mode {0}, exit {1}); see the report for output." -f $pyLauncher, $mk.ExitCode)
            }
            $gp = Join-Path $venvDir "get-pip.py"
            Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $gp -UseBasicParsing
            Invoke-Cmd -Command @($venvPy, $gp) -Label "toolchain_getpip" | Out-Null
            $needGetPip = $true
        }
        if (-not $needGetPip) {
            Invoke-Cmd -Command @($venvPy, "-m", "pip", "install", "--upgrade", "pip") -Label "toolchain_pip_upgrade" | Out-Null
        }
        $inst = Invoke-Cmd -Command @($venvPy, "-m", "pip", "install", "-e", ".[dev]") -Label "toolchain_pip_install_dev"
        if (-not $inst.Ok) { throw "pip install -e .[dev] failed; see the report for output." }
        $kdxExe = Join-Path $venvDir "Scripts\kdx.exe"
        if (-not $IsWin) { $kdxExe = Join-Path $venvDir "bin/kdx" }
        $script:PyRun = @($venvPy)
        $script:KdxRun = @($kdxExe)
        $script:RuffCmd = @($venvPy, "-m", "ruff")
        $script:MypyCmd = @($venvPy, "-m", "mypy")
        $script:PyTestCmd = @($venvPy, "-m", "pytest")
    }
    Invoke-Cmd -Command ($script:PyRun + @("-V")) -Label "toolchain_python_version" | Out-Null
}

function Get-EnvironmentInfo {
    $osLine = ""
    if ($IsWin) {
        $os = Get-CimInstance Win32_OperatingSystem
        $osLine = "{0} (build {1})" -f $os.Caption, $os.Version
    } else {
        $osLine = (Get-Content /etc/os-release | Select-Object -First 1) -replace '^PRETTY_NAME="?|"?$', ''
    }
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
    $hostName = $env:COMPUTERNAME
    if (-not $hostName) { $hostName = [System.Environment]::MachineName }
    Write-Step ("OS: {0}" -f $osLine)
    Write-Step ("Arch: {0}" -f $arch)
    Write-Step ("PowerShell: {0}" -f $PSVersionTable.PSVersion.ToString())
    Write-Step ("Machine: {0}" -f $hostName)
    foreach ($tool in @("uv", "git")) {
        $c = Get-Command $tool -ErrorAction SilentlyContinue
        if ($c) {
            $v = & $c.Source --version 2>&1 | Select-Object -First 1
            Write-Step ("{0}: {1}" -f $tool, [string]$v)
        } else {
            Write-Step ("{0}: not found" -f $tool)
        }
    }
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pyCmd -and $IsWin) { $pyCmd = Get-Command py -ErrorAction SilentlyContinue }
    if ($pyCmd) {
        $v = & $pyCmd.Source --version 2>&1 | Select-Object -First 1
        Write-Step ("python (PATH): {0}" -f [string]$v)
    } else {
        Write-Step "python (PATH): not found"
    }
}

function Test-Daemon {
    $varDir = Join-Path $script:RepoRoot "var"
    New-Item -ItemType Directory -Force -Path $varDir | Out-Null
    $outFile = Join-Path $varDir "win-daemon-stdout.txt"
    $errFile = Join-Path $varDir "win-daemon-stderr.txt"
    $secondOut = Join-Path $varDir "win-second-stdout.txt"
    $secondErr = Join-Path $varDir "win-second-stderr.txt"
    $base = "http://127.0.0.1:" + $Port
    $script:StepStatus["daemon_boot"] = "fail"
    $kdxPrefix = @()
    if ($script:KdxRun.Count -gt 1) { $kdxPrefix = $script:KdxRun[1..($script:KdxRun.Count - 1)] }
    $daemonArgs = $kdxPrefix + @("run", "--no-color", "--no-auth", "--allow-anonymous-shutdown", "--ipc-port", [string]$Port, "--tick-interval", "1.0")
    $proc = Start-Process -FilePath $script:KdxRun[0] -ArgumentList $daemonArgs -WorkingDirectory $script:RepoRoot -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    Add-Report ("daemon started: pid {0}, port {1}" -f $proc.Id, $Port)

    $healthy = $false
    $healthBody = ""
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        $proc.Refresh()
        if ($proc.HasExited) { break }
        try {
            $r = Invoke-WebRequest -Uri ($base + "/health") -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            $healthBody = $r.Content
            $healthy = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }

    if (-not $healthy) {
        $exitNow = $null
        if ($proc.HasExited) { $exitNow = $proc.ExitCode }
        $script:Failures.Add("daemon_boot")
        if ($proc.HasExited) {
            Write-Step ("[FAIL] daemon_boot (process exited {0})" -f $exitNow) "Red"
        } else {
            Write-Step "[FAIL] daemon_boot (no /health answer within 45s)" "Red"
        }
        Add-Report "--- daemon stdout (tail) ---"
        foreach ($l in (Get-TailLines $outFile 40)) { Add-Report $l }
        Add-Report "--- daemon stderr (tail) ---"
        foreach ($l in (Get-TailLines $errFile 40)) { Add-Report $l }
        Add-Report ""
        if (-not $proc.HasExited) {
            try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch { }
        }
        return
    }

    $script:StepStatus["daemon_boot"] = "pass"
    Write-Step "[PASS] daemon_boot (GET /health answered)" "Green"
    Add-Report ("health body: {0}" -f $healthBody)

    try {
        $r = Invoke-WebRequest -Uri ($base + "/api/v1/status") -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        $script:StepStatus["daemon_status_endpoint"] = "pass"
        Write-Step "[PASS] daemon_status_endpoint (GET /api/v1/status)" "Green"
        $body = $r.Content
        if ($body.Length -gt 2000) { $body = $body.Substring(0, 2000) + " ... [truncated]" }
        Add-Report $body
    } catch {
        $script:StepStatus["daemon_status_endpoint"] = "fail"
        $script:Failures.Add("daemon_status_endpoint")
        Write-Step ("[FAIL] daemon_status_endpoint: {0}" -f $_.Exception.Message) "Red"
    }

    Invoke-Cmd -Command ($script:KdxRun + @("status", "--no-color")) -Label "kdx_status_cli" | Out-Null
    Invoke-Cmd -Command ($script:KdxRun + @("--no-color", "--remote", "heartbeat", "status")) -Label "kdx_rpc_remote_heartbeat" | Out-Null

    $script:StepStatus["second_instance_refused"] = "fail"
    $secondArgs = $kdxPrefix + @("run", "--no-color", "--no-ipc", "--tick-interval", "1.0")
    # On Windows PowerShell 5.1 the Process that Start-Process -PassThru returns
    # carries no handle, so .ExitCode stays null forever (verified on this host
    # with 'cmd /c exit 7'), while -Wait would hang the whole harness forever if
    # the instance lock is broken - which is exactly the bug this step must
    # catch. So: start without -Wait, re-open the process by id (a fresh object
    # with a real handle, whose ExitCode is readable), and keep the 20s watchdog
    # with a kill so a stuck second instance can never hang the report.
    $second = Start-Process -FilePath $script:KdxRun[0] -ArgumentList $secondArgs -WorkingDirectory $script:RepoRoot -NoNewWindow -PassThru -RedirectStandardOutput $secondOut -RedirectStandardError $secondErr
    $secondPoll = $second
    try { $secondPoll = [System.Diagnostics.Process]::GetProcessById($second.Id) } catch { }
    $secondExited = $false
    $deadline2 = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline2) {
        try { $secondPoll.Refresh() } catch { }
        try { if ($secondPoll.HasExited) { $secondExited = $true; break } } catch { }
        Start-Sleep -Milliseconds 500
    }
    if ($secondExited) {
        # Exit code sources are platform-specific: on Windows PowerShell 5.1 the
        # PassThru object never populates ExitCode (no handle), while the
        # GetProcessById re-open does; on Unix it is the reverse (the exit status
        # of a process is only readable through the object that spawned it).
        # Read both, take the first non-null answer.
        $secondExit = $null
        foreach ($obj in @($secondPoll, $second)) {
            if ($null -eq $secondExit -and $null -ne $obj) {
                try {
                    $c = $obj.ExitCode
                    if ($null -ne $c) { $secondExit = $c }
                } catch { }
            }
        }
        if ($null -ne $secondExit -and $secondExit -eq 1) {
            $script:StepStatus["second_instance_refused"] = "pass"
            Write-Step "[PASS] second_instance_refused (second daemon exited 1)" "Green"
        } else {
            $script:Failures.Add("second_instance_refused")
            Write-Step ("[FAIL] second_instance_refused (second daemon exited {0}, expected 1)" -f $secondExit) "Red"
        }
    } else {
        $script:Failures.Add("second_instance_refused")
        Write-Step "[FAIL] second_instance_refused (second daemon still running after 20s; killing)" "Red"
        try { Stop-Process -Id $second.Id -Force } catch { }
    }
    Add-Report "--- second instance stderr (tail) ---"
    foreach ($l in (Get-TailLines $secondErr 15)) { Add-Report $l }
    Add-Report ""

    try {
        $r = Invoke-WebRequest -Uri ($base + "/api/v1/shutdown") -Method POST -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        $script:StepStatus["shutdown_post"] = "pass"
        Write-Step ("[PASS] shutdown_post (POST /api/v1/shutdown): {0}" -f $r.Content) "Green"
    } catch {
        $script:StepStatus["shutdown_post"] = "fail"
        $script:Failures.Add("shutdown_post")
        Write-Step ("[FAIL] shutdown_post: {0}" -f $_.Exception.Message) "Red"
    }

    # The main daemon runs without -Wait (the health-check loop needs it live), so on
    # this host its Start-Process ExitCode is null even after it exits. Verify the
    # clean stop the way the chassis actually reports it: the process must be gone,
    # its stderr must carry 'chassis stopped  exit_code=0', and 'kdx status' must
    # agree that no daemon is running.
    $mainExited = $false
    $deadline3 = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline3) {
        $proc.Refresh()
        if ($proc.HasExited) { $mainExited = $true; break }
        Start-Sleep -Milliseconds 500
    }
    $cleanStopLine = $null
    foreach ($l in (Get-TailLines $errFile 40)) {
        if ("$l" -match "chassis stopped\s+exit_code=0") { $cleanStopLine = "$l" }
    }
    if ($mainExited -and $null -ne $cleanStopLine) {
        $script:StepStatus["shutdown_exit"] = "pass"
        Write-Step "[PASS] shutdown_exit (daemon exited; chassis stopped exit_code=0)" "Green"
    } elseif ($mainExited) {
        $script:StepStatus["shutdown_exit"] = "fail"
        $script:Failures.Add("shutdown_exit")
        Write-Step "[FAIL] shutdown_exit (daemon exited but no 'chassis stopped exit_code=0' in stderr)" "Red"
    } else {
        $script:StepStatus["shutdown_exit"] = "fail"
        $script:Failures.Add("shutdown_exit")
        Write-Step "[FAIL] shutdown_exit (daemon still running 25s after shutdown; killing)" "Red"
        try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch { }
    }

    Invoke-Cmd -Command ($script:KdxRun + @("status", "--no-color")) -Label "kdx_status_after_stop" -Info | Out-Null

    Add-Report "--- daemon stdout (tail) ---"
    foreach ($l in (Get-TailLines $outFile 40)) { Add-Report $l }
    Add-Report "--- daemon stderr (tail) ---"
    foreach ($l in (Get-TailLines $errFile 40)) { Add-Report $l }
    Add-Report ""

    Remove-Item -Force -ErrorAction SilentlyContinue $outFile, $errFile, $secondOut, $secondErr
}

function Test-Seed {
    $tmpRoot = $env:TEMP
    if (-not $tmpRoot) { $tmpRoot = "/tmp" }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $seedDir = Join-Path $tmpRoot ("kdx-win-seed-" + $stamp)
    Add-Report ("seed target: {0}" -f $seedDir)
    Invoke-Cmd -Command ($script:KdxRun + @("init", "winseed", "--path", $seedDir, "--yes", "--no-color")) -Label "seed_kdx_init" -Info | Out-Null
    if (Test-Path $seedDir) {
        $expected = @("pyproject.toml", "kdx.yml", "Makefile", "README.md", "AGENTS.md", "src")
        foreach ($e in $expected) {
            if (Test-Path (Join-Path $seedDir $e)) { Add-Report ("  [ok] {0}" -f $e) }
            else { Add-Report ("  [MISSING] {0}" -f $e) }
        }
        $tree = @(Get-ChildItem -Recurse -Force -File $seedDir -ErrorAction SilentlyContinue)
        Add-Report ("seed files: {0}" -f $tree.Count)
        foreach ($f in ($tree | Select-Object -First 80)) {
            Add-Report ("  " + $f.FullName.Substring($seedDir.Length).TrimStart('\', '/'))
        }
    } else {
        Add-Report "  (seed directory was not created)"
    }
    Add-Report ""
    Remove-Item -Recurse -Force $seedDir -ErrorAction SilentlyContinue
}

try {
    $script:RepoRoot = Find-RepoRoot
    Set-Location $script:RepoRoot

    $gitHead = ""
    $gitDirty = "n/a"
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $gitHead = (& git rev-parse --short HEAD 2>$null) -join " "
        $porcelain = & git status --porcelain 2>$null
        if ($porcelain) { $gitDirty = "yes" } else { $gitDirty = "no" }
    }

    Write-Step ("starting: repo {0}, git head {1}, dirty {2}, port {3}" -f $script:RepoRoot, $gitHead, $gitDirty, $Port) "Cyan"

    Write-Step "== environment ==" "Cyan"
    Get-EnvironmentInfo

    Write-Step "== toolchain ==" "Cyan"
    Set-Up-Toolchain

    Write-Step "== static bar (make check) ==" "Cyan"
    Invoke-Cmd -Command ($script:RuffCmd + @("check", "src", "tests")) -Label "lint_ruff" | Out-Null
    Invoke-Cmd -Command $script:MypyCmd -Label "typecheck_mypy_strict" | Out-Null
    # The Makefile floor is 88, but on Windows 22 POSIX-only tests (signal
    # handling, flock, ...) skip, which drops the measured total to ~87.04%.
    # The ratchet rule is "one point under the measured total", so on Windows
    # the honest floor is 87. Linux/macOS keep the full 88.
    $covFloor = 88
    if ($IsWin) { $covFloor = 87 }
    Invoke-Cmd -Command ($script:PyTestCmd + @("-q", "--cov=korodexios_core", "--cov-report=term-missing", "--cov-fail-under=$covFloor")) -Label "tests_pytest_cov" | Out-Null

    if (-not $SkipRuntime) {
        Write-Step "== CLI smoke ==" "Cyan"
        Invoke-Cmd -Command ($script:KdxRun + @("--version")) -Label "kdx_version" | Out-Null
        Invoke-Cmd -Command ($script:KdxRun + @("doctor", "--no-color")) -Label "kdx_doctor" | Out-Null
        Invoke-Cmd -Command ($script:KdxRun + @("modules", "list", "--no-color")) -Label "kdx_modules_list" | Out-Null
        Invoke-Cmd -Command ($script:KdxRun + @("config", "show", "--no-color")) -Label "kdx_config_show" | Out-Null
        Invoke-Cmd -Command ($script:KdxRun + @("contracts", "check", "--no-color")) -Label "kdx_contracts_check" -Info | Out-Null
        Invoke-Cmd -Command ($script:KdxRun + @("run", "--once", "--no-color")) -Label "kdx_run_once" | Out-Null

        Write-Step "== daemon smoke (control plane) ==" "Cyan"
        Test-Daemon

        if ($IncludeSeed) {
            Write-Step "== seed (kdx init) ==" "Cyan"
            Test-Seed
        }
    }
}
catch {
    $script:StepStatus["fatal"] = "fail"
    $script:Failures.Add("fatal")
    Write-Step ("[FAIL] fatal: {0}" -f $_.Exception.Message) "Red"
    Add-Report ""
    Add-Report ("[FATAL] " + $_.Exception.Message)
    Add-Report ($_.ScriptStackTrace)
}
finally {
    $verdict = "PASS"
    if ($script:Failures.Count -gt 0) { $verdict = "FAIL" }
    $fails = $script:Failures -join ", "
    if ($fails -eq "") { $fails = "none" }

    $hostName = $env:COMPUTERNAME
    if (-not $hostName) { $hostName = [System.Environment]::MachineName }
    if (-not $gitHead) { $gitHead = "n/a" }

    $stepsOrdered = [ordered]@{}
    foreach ($k in ($script:StepStatus.Keys | Sort-Object)) {
        $stepsOrdered[$k] = $script:StepStatus[$k]
    }
    $summary = [ordered]@{
        result = $verdict
        failing = @($script:Failures)
        steps = $stepsOrdered
    }

    $header = New-Object System.Collections.Generic.List[string]
    $header.Add("# korodexios-core Windows check report")
    $header.Add("")
    $header.Add(("RESULT: {0}" -f $verdict))
    $header.Add(("Failing steps: {0}" -f $fails))
    $header.Add(("Generated: {0:yyyy-MM-dd HH:mm:ss}" -f (Get-Date)))
    $header.Add(("Host: {0} | PowerShell {1}" -f $hostName, $PSVersionTable.PSVersion.ToString()))
    $header.Add(("Repo: {0}" -f $script:RepoRoot))
    $header.Add(("Git: head {0}, dirty {1}" -f $gitHead, $gitDirty))
    $header.Add("")
    $header.Add("## Step results")
    $header.Add("")
    $header.Add("| step | result |")
    $header.Add("| --- | --- |")
    foreach ($k in ($script:StepStatus.Keys | Sort-Object)) {
        $header.Add(("| {0} | {1} |" -f $k, $script:StepStatus[$k]))
    }
    $header.Add("")
    $header.Add("## Machine summary (JSON)")
    $header.Add("")
    $header.Add([string]($summary | ConvertTo-Json -Depth 5))
    $header.Add("")
    $header.Add("## Log")
    $header.Add("")

    $reportFile = $ReportPath
    if ([string]::IsNullOrWhiteSpace($reportFile)) {
        $anchor = $script:RepoRoot
        if (-not $anchor) { $anchor = Split-Path -Parent $MyInvocation.ScriptName }
        if (-not $anchor) { $anchor = (Get-Location).Path }
        if ($anchor) {
            $reportFile = Join-Path $anchor "var\windows-report.md"
        }
    }
    $reportDir = Split-Path -Parent $reportFile
    if ($reportDir -and -not (Test-Path $reportDir)) {
        New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
    }
    $all = $header + $script:ReportLines
    $enc = New-Object System.Text.UTF8Encoding($false)
    $nl = [string]([char]13) + [char]10
    [System.IO.File]::WriteAllText($reportFile, (($all -join $nl) + $nl), $enc)

    Write-Host ""
    Write-Host ("RESULT: {0}" -f $verdict) $(if ($verdict -eq "PASS") { "Green" } else { "Red" })
    Write-Host ("Report written to: {0}" -f $reportFile)
    Write-Host "Send that file back so the results can be read."
}
