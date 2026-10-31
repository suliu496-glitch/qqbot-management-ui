# Called by the existing desktop launch/stop scripts. Only this bot's Python
# processes are stopped; unrelated Python tools remain running.
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskProcesses = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
foreach ($taskProcess in $taskProcesses) {
    $taskCommand = [string]$taskProcess.CommandLine
    $taskMatches = $taskCommand -match '(?i)(?:^|[\s""\\/])bot\.py(?:[\s""]|$)'
    if ($taskMatches -and ($taskProcess.ExecutablePath -eq $taskPython -or $taskCommand.Contains($taskRoot))) {
        Stop-Process -Id $taskProcess.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
