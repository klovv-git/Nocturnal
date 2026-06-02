# NOCTURNAL — Startup Guide

Everything you need to get the system running after a crash, reboot, or cold start.

---

## 1. After a Normal Reboot

The 7 background services are registered as **Windows Scheduled Tasks** and start
automatically when you log in. You don't need to do anything — just wait ~10 seconds
after login and open your browser.

Open: **http://localhost:5050**

If the page loads, everything is running. Done.

---

## 2. Checking if Services Are Running

Open **PowerShell** (Win+X → Windows PowerShell) and run:

```powershell
Get-ScheduledTask -TaskPath "\Nocturnal\" | Select-Object TaskName, State
```

You should see all 7 tasks in **Running** state:

```
TaskName        State
--------        -----
AIS-Live-Map    Running
AIS-Map         Running
AIS-Tracker     Running
Weather-Fetch   Running
Weather-Map     Running
Weather-Map2    Running
Web-Server      Running
```

If any say **Ready** (not running), go to **Step 3**.

---

## 3. Starting Stopped Services

Open **PowerShell** and run:

```powershell
cd D:\Github\Nocturnal
powershell -ExecutionPolicy Bypass -File start_services.ps1
```

This starts only the tasks that aren't already running. It will print what it started.

Then check **http://localhost:5050** again.

---

## 4. First Time Ever (No Tasks Registered Yet)

If you've never run the setup, or after reinstalling Windows:

```powershell
cd D:\Github\Nocturnal
powershell -ExecutionPolicy Bypass -File register_tasks.ps1
```

This registers all 7 tasks in Task Scheduler AND starts them immediately.
You only need to run this **once per Windows installation**.

---

## 5. What Each Service Does

| What you see in Task Scheduler | What it actually does |
|---|---|
| **AIS-Tracker** | Polls live ship AIS positions every 60 seconds and saves them to the database. If this is stopped, no new ship data is collected. |
| **Web-Server** | Runs the website at http://localhost:5050. If stopped, the browser shows "site can't be reached". |
| **Weather-Fetch** | Downloads weather data every 30 minutes from StormGlass. |
| **AIS-Live-Map** | Rebuilds the live ship positions map every 5 minutes. |
| **AIS-Map** | Rebuilds the full track history map every 12 hours. |
| **Weather-Map** | Rebuilds the weather chart every 20 minutes. |
| **Weather-Map2** | Rebuilds the weather + AIS combined map every 20 minutes. |

---

## 6. Checking the Batch Download

A large Sentinel-1 download (~163 scenes, ~270 GB) may be in progress.
Check if it's still running:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*batch_download*" -and $_.CommandLine -notlike "*NonInteractive*" }
```

If **nothing is returned** — the download has stopped. Look at the log to see where it got to:

```powershell
Get-Content D:\Github\Nocturnal\download.log -Tail 10
```

To see how many scenes have completed:

```powershell
(Get-ChildItem D:\Github\Nocturnal\sentinel_data -Filter "*.SAFE").Count
```

### Resuming a stopped download

First remove any partial zip file left behind:

```powershell
Get-ChildItem D:\Github\Nocturnal\sentinel_data -Filter "*.zip"
```

If any `.zip` files appear, delete them:

```powershell
Get-ChildItem D:\Github\Nocturnal\sentinel_data -Filter "*.zip" | Remove-Item -Force
```

Then restart the download. Open a **new PowerShell window** and run:

```powershell
cd D:\Github\Nocturnal
$host.UI.RawUI.WindowTitle = "Nocturnal - Batch Download"
.\venv\Scripts\python.exe -u batch_download.py --after 2026-04-23 --before 2026-06-02 2>&1 | Tee-Object download.log
```

Already-downloaded scenes are automatically skipped. It picks up where it left off.

---

## 7. Processing New Sentinel Scenes (After Download)

Once new `.SAFE` folders appear in `sentinel_data/`, run the detection pipeline:

**Open a new PowerShell window** and:

```powershell
cd D:\Github\Nocturnal
.\venv\Scripts\python.exe run_pipeline.py --all
```

This runs 4 phases per scene (SAR extraction → YOLO detection → AIS matching → chip images).
It takes roughly 5–15 minutes per scene depending on GPU. Watch the window for progress.

When the pipeline finishes, rebuild the timeline map:

```powershell
.\venv\Scripts\python.exe ais_timeline_map.py --all
```

Then refresh **http://localhost:5050** — new satellite passes will appear on the left.

---

## 8. Rebuilding Maps Manually

If any map looks outdated, rebuild it:

| Map | Command |
|---|---|
| Timeline map (dark vessels) | `.\venv\Scripts\python.exe ais_timeline_map.py --all` |
| Live AIS positions | `.\venv\Scripts\python.exe ais_live_map.py --once` |
| Full AIS track history | `.\venv\Scripts\python.exe ais_map.py --once` |
| Weather chart | `.\venv\Scripts\python.exe weather_map.py --once` |
| Weather + AIS combined | `.\venv\Scripts\python.exe weather_map2.py --once` |

All commands must be run from `D:\Github\Nocturnal` using the venv Python.

---

## 9. Checking the AIS Tracker is Actually Collecting

```powershell
cd D:\Github\Nocturnal
.\venv\Scripts\python.exe -c "
import sqlite3, time
conn = sqlite3.connect('ais_memory.db')
t1 = conn.execute('SELECT MAX(ts_epoch) FROM positions').fetchone()[0]
time.sleep(70)
t2 = conn.execute('SELECT MAX(ts_epoch) FROM positions').fetchone()[0]
print('New pings in last 70s:', 'YES' if t2 > t1 else 'NO - tracker may be stopped')
conn.close()
"
```

If it says **NO**, restart the AIS-Tracker task:

```powershell
Stop-ScheduledTask -TaskName "AIS-Tracker" -TaskPath "\Nocturnal\"
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "AIS-Tracker" -TaskPath "\Nocturnal\"
```

---

## 10. Full Cold Start Checklist

If everything is down and you need to start from zero:

```
[ ] Open PowerShell
[ ] cd D:\Github\Nocturnal
[ ] powershell -ExecutionPolicy Bypass -File start_services.ps1
[ ] Wait 10 seconds
[ ] Open http://localhost:5050 in browser — page should load
[ ] Check: Get-ScheduledTask -TaskPath "\Nocturnal\" | Select TaskName, State
[ ] All 7 tasks show "Running"? You're done.
[ ] Batch download stopped? See Section 6 above.
[ ] New scenes to process? See Section 7 above.
```

---

## 11. Important Paths

| What | Where |
|---|---|
| Project root | `D:\Github\Nocturnal` |
| Python to use | `D:\Github\Nocturnal\venv\Scripts\python.exe` |
| Web UI | http://localhost:5050 |
| AIS database | `D:\Github\Nocturnal\ais_memory.db` (~41 GB) |
| Downloaded SAR scenes | `D:\Github\Nocturnal\sentinel_data\` |
| Download progress log | `D:\Github\Nocturnal\download.log` |
| Pipeline progress log | `D:\Github\Nocturnal\pipeline.log` |
| CDSE login credentials | `C:\Users\Exposition\.cdse_credentials` |
| AISHub rate-limit file | `C:\Users\Exposition\.aishub_last_poll` |

---

## 12. If the Web Server Isn't Responding

Check if port 5050 is in use by something else:

```powershell
netstat -ano | Select-String ":5050"
```

If something else is on 5050, find what it is:

```powershell
$pid = (netstat -ano | Select-String ":5050 .*LISTENING").ToString().Split()[-1]
Get-Process -Id $pid
```

---

## 13. Never Do These Things

- ❌ Don't kill `C:\Users\...\WindowsApps\python.exe` processes — these are legitimate worker processes spawned by the venv Python. Killing them kills the actual service.
- ❌ Don't run scripts using `python script.py` (bare command) — always use the full venv path: `.\venv\Scripts\python.exe script.py`
- ❌ Don't copy `ais_memory.db` while the tracker is running without first running `PRAGMA wal_checkpoint(TRUNCATE)` — the WAL file may not be flushed.
- ❌ Don't run `run_pipeline.py --all` on already-processed scenes without checking — the YOLO step uses plain INSERT and creates duplicate detection rows.
