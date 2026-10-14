# One-command setup and launch for Windows (PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File setup_and_run.ps1
#
# Creates a virtual environment, installs the kit, builds the dummy SAP data,
# checks every rule, runs the end-to-end demo, then opens the planning
# assistant. No API key is needed; without one the assistant runs offline.
# Pass -NoChat to stop before the assistant opens.

param([switch]$NoChat)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($title, [scriptblock]$cmd) {
    Write-Host ""
    Write-Host "== $title" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Step failed: $title (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# Windows limits file paths to 260 characters; Python packages inside .venv add
# about 150, so a deeply nested folder makes libraries fail to load.
if ($PSScriptRoot.Length -gt 100) {
    Write-Host "This folder's path is too long for Windows ($($PSScriptRoot.Length) characters):" -ForegroundColor Red
    Write-Host "  $PSScriptRoot"
    Write-Host "Move the sap-claude-agent-kit folder somewhere shorter, for example C:\sap-claude-agent-kit or your Downloads folder, and run this again."
    exit 1
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python was not found. Install Python 3.10 or newer from python.org, tick 'Add python.exe to PATH', then run this again." -ForegroundColor Red
    exit 1
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Step "Creating a virtual environment" { python -m venv .venv }
}
Step "Installing the kit (first time takes a minute)" { & $venvPy -m pip install --quiet --disable-pip-version-check -e ".[dev]" }
Step "Building the dummy SAP data" { & $venvPy -m sapkit generate }
Step "Checking every rule against the built-in answers" { & $venvPy -m sapkit verify }
New-Item -ItemType Directory -Force out | Out-Null
Step "Running the end-to-end demo (outreach, replies, rerun)" { & $venvPy -m sapkit demo --offline | Out-File -Encoding utf8 out\demo.log }
Step "Writing the report" { & $venvPy -m sapkit report }

# Let Claude Code use the tools when it is opened in this folder (no API key needed).
$mcpJson = '{"mcpServers": {"sapkit": {"command": "' + ($venvPy.Replace('\', '/')) + '", "args": ["-m", "sapkit", "mcp"]}}}'
[IO.File]::WriteAllText((Join-Path $PSScriptRoot ".mcp.json"), $mcpJson, (New-Object Text.UTF8Encoding $false))

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "  Supplier drafts:  out\drafts\"
Write-Host "  Report:           out\report\summary.md"
Write-Host "  Run the tests:    .venv\Scripts\python -m pytest"
Write-Host "  Use real Claude:  run 'claude' in this folder and approve the 'sapkit' tools"

if (-not $NoChat) {
    Write-Host ""
    & $venvPy -m sapkit ask
}
