"""Read existing Windows VSS snapshots for a drive-letter folder."""
import json
import os
from pathlib import Path
import re
import subprocess

from recycle_bin import ordinary
from server import LOCK

QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$v = Get-CimInstance Win32_Volume | Where-Object DriveLetter -eq $env:DIGGER_VOLUME
if (!$v) { throw 'The selected volume is unavailable.' }
$rows = @(Get-CimInstance Win32_ShadowCopy | Where-Object VolumeName -eq $v.DeviceID |
    Sort-Object InstallDate -Descending | ForEach-Object {
        [pscustomobject]@{device=$_.DeviceObject; date=$_.InstallDate.ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss')}
    })
ConvertTo-Json -InputObject $rows -Compress
"""


def snapshots(scope, job):
    binary = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with subprocess.Popen([str(binary), "-NoProfile", "-NonInteractive", "-Command", QUERY],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
                          env={**os.environ, "DIGGER_VOLUME": scope.drive}) as process:
        with LOCK:
            job["process"] = process
            if job["status"] == "stopping":
                process.terminate()
        try:
            data, errors = process.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise OSError("Windows timed out listing shadow copies.")
        finally:
            with LOCK:
                job.pop("process", None)
    if job["status"] == "stopping":
        return []
    if process.returncode:
        raise OSError("Windows could not list shadow copies. " + errors.decode("utf-8", "replace")[:500])
    return json.loads(data.decode("utf-8-sig"))


def scan_versions(scope, job):
    job["phase"] = "Looking up existing Windows shadow copies"
    rows = snapshots(scope, job)
    job["snapshots"] = len(rows)
    job["unreadable"] = 0

    def inaccessible(_error):
        job["unreadable"] += 1

    for row in rows:
        if job["status"] == "stopping":
            return
        if not re.fullmatch(r"\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy[0-9]+", row["device"], re.I):
            raise OSError("Windows returned an unrecognized shadow-copy device.")
        root = Path(row["device"] + "\\")
        date = row["date"]
        job["phase"] = f"Searching snapshot {date}"
        folder = root
        try:
            for part in scope.parts[1:]:
                folder /= part
                if not ordinary(folder):
                    break
            else:
                for directory, dirs, names in os.walk(folder, onerror=inaccessible):
                    if job["status"] == "stopping":
                        return
                    base = Path(directory)
                    def safe(name):
                        try:
                            return ordinary(base / name)
                        except OSError as exc:
                            inaccessible(exc)
                            return False
                    dirs[:] = [name for name in dirs if safe(name)]
                    for name in names:
                        if job["status"] == "stopping":
                            return
                        if safe(name):
                            path = base / name
                            job["matches"].append((path, scope / path.relative_to(folder), None, date))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            inaccessible(exc)
