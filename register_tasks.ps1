# register_tasks.ps1 — Register all NOCTURNAL services as Windows Scheduled Tasks.
#
# Run ONCE (as the current user, no elevation needed for user-context tasks):
#   powershell -ExecutionPolicy Bypass -File D:\Github\Nocturnal\register_tasks.ps1
#
# To remove all tasks later:
#   Get-ScheduledTask -TaskPath "\Nocturnal\" | Unregister-ScheduledTask -Confirm:$false
#
# To check status:
#   Get-ScheduledTask -TaskPath "\Nocturnal\" | Select-Object TaskName, State

$base    = "D:\Github\Nocturnal"
$py      = "$base\venv\Scripts\python.exe"
$user    = $env:USERDOMAIN + "\" + $env:USERNAME
$folder  = "\Nocturnal"

# ── NOTE on Windows Store Python ─────────────────────────────────────────────
# The venv was created from the Windows Store Python (pyvenv.cfg home =
# C:\...\WindowsApps\PythonSoftwareFoundation.Python.3.10_...).
# On Windows, this Python uses an App Execution Alias: running venv\python.exe
# spawns a stub that in turn launches the real interpreter at the WindowsApps
# path.  Both appear in the process list — this is ONE process, not a duplicate.
# Do NOT kill WindowsApps python.exe processes; they are the real workers.
#
# MultipleInstances = IgnoreNew in the task settings below prevents genuine
# duplicates from being started if a task is triggered twice.

# ── Ensure task folder exists ─────────────────────────────────────────────────
$schedSvc = New-Object -ComObject Schedule.Service
$schedSvc.Connect()
$root = $schedSvc.GetFolder("\")
try { $root.GetFolder("Nocturnal") | Out-Null }
catch { $root.CreateFolder("Nocturnal") | Out-Null; Write-Host "Created task folder \Nocturnal" }

# ── Service definitions ───────────────────────────────────────────────────────
$services = @(
    @{ Name = "AIS-Tracker";    Script = "aishub_tracker.py" },
    @{ Name = "Web-Server";     Script = "nocturnal_server.py" },
    @{ Name = "Weather-Fetch";  Script = "weather_fetch.py --daemon" },
    @{ Name = "AIS-Live-Map";   Script = "ais_live_map.py" },
    @{ Name = "AIS-Map";        Script = "ais_map.py" },
    @{ Name = "Weather-Map";    Script = "weather_map.py" },
    @{ Name = "Weather-Map2";   Script = "weather_map2.py" }
)

foreach ($svc in $services) {
    $taskName = $svc.Name
    $script   = $svc.Script

    # Remove existing task with same name (clean re-register)
    Unregister-ScheduledTask -TaskName $taskName -TaskPath $folder -Confirm:$false -ErrorAction SilentlyContinue

    # Action: run the venv Python with the script, working dir = project root
    $action = New-ScheduledTaskAction `
        -Execute $py `
        -Argument $script `
        -WorkingDirectory $base

    # Trigger: at user logon
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

    # Settings: restart up to 3 times on failure (1 min gap), run indefinitely
    # DisallowStartIfOnBatteries:$false + StopIfGoingOnBatteries:$false ensures
    # tasks keep running if the laptop is unplugged (default Windows behaviour
    # is to stop all tasks on battery, which caused silent service outages).
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -DisallowStartIfOnBatteries:$false

    # Principal: run as current user, only when logged in
    $principal = New-ScheduledTaskPrincipal `
        -UserId $user `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName  $taskName `
        -TaskPath  $folder `
        -Action    $action `
        -Trigger   $trigger `
        -Settings  $settings `
        -Principal $principal `
        -Force | Out-Null

    Write-Host "Registered: $folder\$taskName  ($script)"
}

Write-Host ""
Write-Host "All tasks registered under \Nocturnal\ in Task Scheduler."
Write-Host "They will start automatically at next login."
Write-Host ""
Write-Host "Starting them NOW without waiting for a reboot..."

foreach ($svc in $services) {
    Start-ScheduledTask -TaskName $svc.Name -TaskPath $folder
    Write-Host "  Started: $($svc.Name)"
}

Write-Host ""
Write-Host "Done. Verify at http://localhost:5050"
Write-Host "To see task status: Get-ScheduledTask -TaskPath '\Nocturnal\' | Select TaskName, State"
