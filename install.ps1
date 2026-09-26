param(
    [string]$IndexUrl
)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments, [string]$Step)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE). Fix the error above and run install.cmd again."
    }
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $pythonCommand = $launcher.Source
        $pythonPrefix = @('-3')
    } else {
        $launcher = Get-Command python -ErrorAction SilentlyContinue
        if (-not $launcher) {
            throw 'Python was not found. Install Python 3.10+ and enable the Python launcher or PATH option.'
        }
        $pythonCommand = $launcher.Source
        $pythonPrefix = @()
    }
    Invoke-Checked -Executable $pythonCommand -Arguments ($pythonPrefix + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')) -Step 'Python 3.10+ check'
    Invoke-Checked -Executable $pythonCommand -Arguments ($pythonPrefix + @('-m', 'venv', (Join-Path $PSScriptRoot '.venv'))) -Step 'Virtual environment creation'
}

Invoke-Checked -Executable $venvPython -Arguments @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)') -Step 'Virtual environment Python version check'

# An interrupted venv creation can leave python.exe in place without pip.
# Repair only this local environment; do not delete it or change system Python.
Write-Host 'Checking pip in the local Python environment...'
& $venvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip') is not None else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'pip is missing. Restoring it with the bundled ensurepip module...'
    Invoke-Checked -Executable $venvPython -Arguments @('-m', 'ensurepip', '--upgrade', '--default-pip') -Step 'Local pip repair'
}
Invoke-Checked -Executable $venvPython -Arguments @('-m', 'pip', '--version') -Step 'Local pip verification'

$pipArguments = @('-m', 'pip', 'install', '-r', (Join-Path $PSScriptRoot 'requirements.txt'), '--timeout', '30', '--retries', '2')
if (-not [string]::IsNullOrWhiteSpace($IndexUrl)) {
    $pipArguments += @('--index-url', $IndexUrl)
}
Write-Host 'Installing Python packages into .venv...'
Invoke-Checked -Executable $venvPython -Arguments $pipArguments -Step 'Package installation'
Write-Host 'Installing Playwright Chromium...'
Invoke-Checked -Executable $venvPython -Arguments @('-m', 'playwright', 'install', 'chromium') -Step 'Chromium installation'
Write-Host 'Installation complete. Run start.cmd.' -ForegroundColor Green
