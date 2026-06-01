# start_services.ps1 — Launch all NOCTURNAL background services from D:\Github\Nocturnal
# Run this once after a reboot or if any service goes down.
# Always uses the project venv Python — never the Windows Store python.exe.

$base = "D:\Github\Nocturnal"
$py   = "$base\venv\Scripts\python.exe"

# ── Kill any stale processes (C: WindowsApps Python duplicates) ───────────────
Write-Host "Cleaning up any stale Python processes..."
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -like "*python*" -and $_.CommandLine -like "*WindowsApps*"
} | ForEach-Object {
    Write-Host "  Killing PID $($_.ProcessId) (C: duplicate)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

# ── Start each service in its own titled window ───────────────────────────────
$services = @(
    @{ title = "Nocturnal - AIS Tracker";   cmd = "aishub_tracker.py" },
    @{ title = "Nocturnal - Web Server";     cmd = "nocturnal_server.py" },
    @{ title = "Nocturnal - Weather Fetch";  cmd = "weather_fetch.py --daemon" },
    @{ title = "Nocturnal - AIS Live Map";   cmd = "ais_live_map.py" },
    @{ title = "Nocturnal - AIS Map";        cmd = "ais_map.py" },
    @{ title = "Nocturnal - Weather Map";    cmd = "weather_map.py" },
    @{ title = "Nocturnal - Weather Map 2";  cmd = "weather_map2.py" }
)

foreach ($svc in $services) {
    $cmd = "Set-Location '$base'; `$host.UI.RawUI.WindowTitle = '$($svc.title)'; & '$py' $($svc.cmd)"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WindowStyle Normal
    Write-Host "  Started: $($svc.title)"
}

Write-Host ""
Write-Host "All services started. Web UI at http://localhost:5050"
Write-Host ""
Write-Host "To run the pipeline on new scenes:"
Write-Host "  & '$py' run_pipeline.py --all"
Write-Host "  & '$py' ais_timeline_map.py --all"
