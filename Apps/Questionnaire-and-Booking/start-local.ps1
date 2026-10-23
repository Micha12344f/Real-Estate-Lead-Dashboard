<#
  start-local.ps1 — start and supervise the TVE local stack without Docker serving the dashboard.

  Architecture (this host only):
    * Docker Compose runs the data + logic tier: the six Postgres containers, Mailpit and
      app-function-logic (the API on 127.0.0.1:8001).
    * The web frontend — Booking 3000, Questionnaire 3001 and the Lead Intelligence
      dashboard 3002 — is served by native Node (`frontend/serve-local.js`), NOT by the
      `web-frontend` container. UI edits therefore take effect on refresh with no image
      rebuild, which is the whole point of leaving Compose for the dashboard.

  Why the supervisor: these services previously ran in throwaway terminals, so an
  interrupted command killed them and the dashboard appeared to "crash". This script owns
  the lifecycle: it starts the stack, then restarts anything that exits.

  Usage:
    powershell -ExecutionPolicy Bypass -File start-local.ps1        # start + supervise
    powershell -ExecutionPolicy Bypass -File start-local.ps1 -Once  # start, verify, exit
#>

[CmdletBinding()]
param(
  [switch]$Once,
  [int]$DockerReadyTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'

$App      = $PSScriptRoot
$Backend  = Join-Path $App 'backend'
$Frontend = Join-Path $App 'frontend'
$Compose  = Join-Path $Backend 'docker\docker-compose.yml'
$EnvFile  = Join-Path $Backend '.env'
$LogDir   = Join-Path $App '.local-run'

$ProjectName = 'tve-questionnaire-booking'
$BackendUrl  = 'http://127.0.0.1:8001'
$BackendPort = 8001
$WebPorts    = @(3000, 3001, 3002)

# Every Compose service except the containerised frontend we replace with Node.
$ComposeServices = @(
  'pg-contacts', 'pg-enquiries', 'pg-responses', 'pg-tokens', 'pg-settings', 'pg-sandbox',
  'mock-inbox', 'imap-intake', 'app-function-logic'
)

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
  Write-Host ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message)
}

function Import-DotEnv([string]$Path) {
  if (-not (Test-Path $Path)) { Write-Log "No env file at $Path; using defaults."; return }
  $count = 0
  foreach ($line in Get-Content -LiteralPath $Path) {
    $text = $line.Trim()
    if (-not $text -or $text.StartsWith('#')) { continue }
    $index = $text.IndexOf('=')
    if ($index -lt 1) { continue }
    $name = $text.Substring(0, $index).Trim()
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
    $value = $text.Substring($index + 1).Trim()
    if ($value.Length -ge 2 -and $value[0] -eq $value[$value.Length - 1] -and $value[0] -in '"', "'") {
      $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    $count++
  }
  Write-Log "Loaded $count variables from backend/.env"
}

function Get-ExistingFrontend {
  $listeners = @(Get-NetTCPConnection -LocalPort $WebPorts -State Listen -ErrorAction SilentlyContinue)
  if (-not $listeners.Count) { return $null }
  $processIds = @($listeners.OwningProcess | Sort-Object -Unique)
  $ports = @($listeners.LocalPort | Sort-Object -Unique)
  if ($processIds.Count -eq 1 -and $ports.Count -eq $WebPorts.Count) {
    $info = Get-CimInstance Win32_Process -Filter "ProcessId=$($processIds[0])"
    if ($info.Name -eq 'node.exe' -and $info.CommandLine -match '\bserve-local\.js(?:\s|"|$)' -and (Test-DashboardReady)) {
      Write-Log "Reusing the healthy native frontend (pid $($processIds[0]))."
      return Get-Process -Id $processIds[0]
    }
  }
  throw 'Frontend ports are occupied but the existing native dashboard is not healthy; inspect .local-run logs and port ownership.'
}

function Test-DockerDaemon {
  # Bound the CLI itself: an unresponsive pipe must not hang the supervisor.
  $probe = [System.Diagnostics.Process]::new()
  $probe.StartInfo.FileName = 'docker'
  $probe.StartInfo.Arguments = "version --format '{{.Server.Version}}'"
  $probe.StartInfo.UseShellExecute = $false
  $probe.StartInfo.CreateNoWindow = $true
  $probe.StartInfo.RedirectStandardOutput = $true
  $probe.StartInfo.RedirectStandardError = $true
  try {
    $null = $probe.Start()
    if (-not $probe.WaitForExit(10000)) {
      $probe.Kill()
      return $false
    }
    return $probe.ExitCode -eq 0
  } catch {
    return $false
  } finally {
    $probe.Dispose()
  }
}

function Wait-DockerDaemon([int]$TimeoutSeconds) {
  if (Test-DockerDaemon) { return $true }
  $desktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
  if (Test-Path $desktop) {
    Write-Log 'Docker daemon is down; launching Docker Desktop.'
    Start-Process -FilePath $desktop
  } else {
    Write-Log 'Docker daemon is down and Docker Desktop was not found.'
  }
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if (Test-DockerDaemon) {
      Write-Log ('Docker daemon ready ({0}).' -f (docker version --format '{{.Server.Version}}'))
      return $true
    }
  }
  Write-Log "Docker daemon did not become ready within ${TimeoutSeconds}s."
  return $false
}

function Start-DataStack {
  if (-not (Wait-DockerDaemon -TimeoutSeconds $DockerReadyTimeoutSeconds)) {
    throw 'Docker daemon unavailable, so the database tier cannot start.'
  }
  Write-Log 'Bringing up the Compose data + logic services.'
  # Compose writes progress/warnings to stderr with exit code 0; check $LASTEXITCODE rather
  # than letting ErrorActionPreference turn that noise into a terminating error.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & docker compose --project-name $ProjectName --env-file $EnvFile -f $Compose up -d @ComposeServices
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previous
  }
  if ($exitCode -ne 0) { throw "docker compose up failed with exit code $exitCode." }
  Repair-ApiPortBinding
}

# Verify the actual loopback binding, including null/missing port mappings.
function Repair-ApiPortBinding {
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $published = & docker inspect tve-app-function-logic --format '{{json .NetworkSettings.Ports}}' 2>$null
    if ($LASTEXITCODE -ne 0) { return }
    $bindings = ($published | ConvertFrom-Json).'8000/tcp'
    if (@($bindings | Where-Object { $_.HostIp -eq '127.0.0.1' -and $_.HostPort -eq "$BackendPort" }).Count) { return }
    Write-Log 'API container has no published port on 8001; recreating it.'
    & docker compose --project-name $ProjectName --env-file $EnvFile -f $Compose up -d --no-deps --force-recreate app-function-logic *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Could not repair the API port binding.' }
  } finally {
    $ErrorActionPreference = $previous
  }
}

function Test-BackendReady {
  try {
    $response = Invoke-WebRequest -Uri "$BackendUrl/healthz" -UseBasicParsing -TimeoutSec 3
    return $response.StatusCode -eq 200 -and ($response.Content | ConvertFrom-Json).status -eq 'ok'
  } catch {
    return $false
  }
}

function Wait-BackendReady([int]$TimeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-BackendReady) { return $true }
    Start-Sleep -Seconds 2
  }
  return $false
}

function Start-Frontend {
  $stdout = Join-Path $LogDir 'frontend.out.log'
  $stderr = Join-Path $LogDir 'frontend.err.log'
  Write-Log "Starting the native frontend proxy on ports $($WebPorts -join ', ')"
  Start-Process -FilePath 'node' -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
    -ArgumentList @('serve-local.js') `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
}

function Test-DashboardReady {
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri 'http://127.0.0.1:3002/sandbox/api/leads' -UseBasicParsing -TimeoutSec 8
      if ($response.StatusCode -eq 200) { return $true }
    } catch { }
    Start-Sleep -Seconds 2
  }
  return $false
}

Import-DotEnv -Path $EnvFile
[Environment]::SetEnvironmentVariable('PYTHONUNBUFFERED', '1', 'Process')
[Environment]::SetEnvironmentVariable('APP_FUNCTION_LOGIC_URL', $BackendUrl, 'Process')
[Environment]::SetEnvironmentVariable('APP_LISTEN_HOST', '127.0.0.1', 'Process')

# Never kill the owner of 8001: on Windows this can be Docker's forwarding process.
Start-DataStack
if (-not (Wait-BackendReady -TimeoutSeconds 120)) { throw 'Backend /healthz did not become healthy within 120 seconds.' }

$frontend = Get-ExistingFrontend
$frontendOwned = $false
if (-not $frontend) {
  $frontend = Start-Frontend
  $frontendOwned = $true
}
if (-not (Test-DashboardReady)) { throw 'Dashboard leads API failed; inspect backend and frontend logs.' }

if ($Once) {
  Start-Sleep -Seconds 4
  Write-Log ('Backend reachable:    {0}' -f (Test-BackendReady))
  Write-Log ('Dashboard responding: {0}' -f (Test-DashboardReady))
  return
}

# Supervise the native proxy and the Compose tier.
try {
  while ($true) {
    Start-Sleep -Seconds 5
    if ($frontend.HasExited) {
      Write-Log "Frontend proxy exited (code $($frontend.ExitCode)); restarting."
      $frontend = Start-Frontend
      $frontendOwned = $true
    }
    if (-not (Test-BackendReady)) {
      Write-Log 'Backend health check failed; re-applying the Compose tier.'
      try { Start-DataStack } catch { Write-Log "Compose recovery failed: $($_.Exception.Message)" }
    }
  }
} finally {
  if ($frontendOwned -and $frontend -and -not $frontend.HasExited) {
    Write-Log 'Shutting down the supervised frontend proxy.'
    Stop-Process -Id $frontend.Id -Force -ErrorAction SilentlyContinue
  }
}
