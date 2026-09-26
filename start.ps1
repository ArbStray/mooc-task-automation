param(
    [string]$Url,
    [ValidateRange(1, 100000)]
    [int]$MaxTasks = 100,
    [switch]$AutoAnswer,
    [switch]$AIAnswer,
    [switch]$NoAutoSubmit,
    [switch]$Headless,
    [string]$Config = (Join-Path $PSScriptRoot 'config.example.json'),
    [string]$Answers
)

$ErrorActionPreference = 'Stop'
$hasExplicitAnswers = $PSBoundParameters.ContainsKey('Answers')
if ($hasExplicitAnswers -and [string]::IsNullOrWhiteSpace($Answers)) {
    throw '-Answers must specify a non-empty answer file path.'
}
if ($hasExplicitAnswers -and -not $AutoAnswer) {
    throw '-Answers requires -AutoAnswer; the default mode answers exercises manually.'
}
if ($AIAnswer -and -not $AutoAnswer -and $hasExplicitAnswers) {
    throw 'Pure AI mode does not use answer books; add -AutoAnswer for book + AI fallback.'
}
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The local Python environment is missing. Run install.cmd first.'
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    if ($Headless) {
        throw 'Headless mode requires -Url and an existing login session.'
    }
    $Url = Read-Host 'Paste the full course chapter URL'
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    throw 'A course URL is required.'
}
$uri = $null
if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @('http', 'https')) {
    throw 'The course URL must start with http:// or https://.'
}
$inputPaths = @($Config)
if ($hasExplicitAnswers) { $inputPaths += $Answers }
foreach ($inputPath in $inputPaths) {
    if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) {
        throw "Input file not found: $inputPath"
    }
}
$runArguments = @(
    (Join-Path $PSScriptRoot 'auto_tasks.py'), '--url', $Url,
    '--config', $Config, '--max-tasks', $MaxTasks,
    '--user-data-dir', (Join-Path $PSScriptRoot '.course-browser')
)
if ($AutoAnswer) {
    $runArguments += '--auto-answer'
    if ($hasExplicitAnswers) {
        $runArguments += @('--answers', $Answers)
    } elseif (-not $Headless) {
        $runArguments += '--choose-answers'
    }
    if (-not $NoAutoSubmit) { $runArguments += '--auto-submit' }
}
if ($AIAnswer) {
    $runArguments += '--ai-answer'
    if (-not $AutoAnswer) {
        if ($NoAutoSubmit) {
            Write-Host 'Pure AI mode: answers are filled in only. Review in the browser and submit manually.'
        } else {
            $runArguments += '--auto-submit'
            Write-Host 'Pure AI mode: exercises are answered by the AI API and submitted automatically.'
        }
    }
}
if ($Headless) { $runArguments += '--headless' } else { $runArguments += '--headed' }
if (-not $AutoAnswer -and -not $AIAnswer) {
    Write-Host 'Manual answering mode: videos and readings are automated; exercises are yours. Press Enter in this terminal when an exercise is done.'
}
& $pythonPath @runArguments
$runExitCode = $LASTEXITCODE
if ($runExitCode -ne 0) {
    Write-Host "Automation paused or failed (exit code $runExitCode). Check the message above."
}
exit $runExitCode
