# start_services.ps1 — Start all NOCTURNAL services via their Scheduled Tasks.
#
# NOTE: Do NOT use this to kill "C: WindowsApps" Python processes.
# The venv was built on Windows Store Python — each service legitimately shows
# two processes (the stub launcher + the real interpreter at WindowsApps path).
# They are ONE process, not duplicates.
#
# Preferred usage: rely on Task Scheduler (run register_tasks.ps1 once).
# Use this script only if a specific service has crashed and Task Scheduler
# hasn't restarted it yet (it retries up to 3x with a 1-minute gap).
#
# Run: powershell -ExecutionPolicy Bypass -File D:\Github\Nocturnal\start_services.ps1

$folder = "\Nocturnal"

# Check if tasks are registered
$tasks = Get-ScheduledTask -TaskPath $folder -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "Tasks not registered yet. Run register_tasks.ps1 first."
    exit 1
}

# Start any task that is not already Running
foreach ($task in $tasks) {
    if ($task.State -ne "Running") {
        Start-ScheduledTask -TaskName $task.TaskName -TaskPath $folder
        Write-Host "  Started: $($task.TaskName)"
    } else {
        Write-Host "  Already running: $($task.TaskName)"
    }
}

Write-Host ""
Write-Host "Status:"
Get-ScheduledTask -TaskPath $folder | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Web UI: http://localhost:5050"
