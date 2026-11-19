#!/usr/bin/env python3
"""
bklock.py — applica ricorsivamente il flag IMMUTABLE (chattr +i)
a tutti i file del backup lato TARGET.
Esclude i file vmbackup.json e le cartelle VMDEF (devono restare scrivibili).
Da mettere in cron.

© 2025 — GPLv3
"""

import configparser
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

SKIP_FILES = {"vmbackup.json"}
SKIP_DIRS  = {"VMDEF"}


# ── SIEM: una riga di syslog (inoltrata dal NAS al SIEM come programma siem-*) ──

def _siem_fmt(value) -> str:
    s = " ".join(str(value).split())[:500]
    if s == "" or any(c in s for c in ' "='):
        return '"' + s.replace('"', "'") + '"'
    return s


def siem(event: str, job: str, **fields):
    """logger -t siem-cephbackup; non deve mai far fallire lo script."""
    line = " ".join([f"event={_siem_fmt(event)}", f"job={_siem_fmt(job)}"] +
                    [f"{k}={_siem_fmt(v)}" for k, v in fields.items()])
    try:
        subprocess.run(["logger", "-t", "siem-cephbackup", "-p", "authpriv.notice", line],
                       timeout=5, check=False)
    except Exception:
        pass


def job_name() -> str:
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return cfg.get("job", "jobname", fallback="") or SCRIPT_DIR.name


def chattr(flag: str, path: Path) -> bool:
    return subprocess.run(["chattr", flag, str(path)], check=False).returncode == 0


STATS = {"files": 0, "errors": 0}


def recurse_folder(path: Path):
    for entry in sorted(path.iterdir()):
        if entry.is_dir():
            if entry.name in SKIP_DIRS:
                # bkexec.py deve poter aggiornare gli XML: sblocca anche quelli bloccati in passato
                print(f"skip immutable (excluded dir): {entry}")
                subprocess.run(["chattr", "-R", "-i", str(entry)], check=False)
                continue
            recurse_folder(entry)
        else:
            if entry.name in SKIP_FILES:
                print(f"skip immutable (excluded file): {entry}")
                continue
            print(f"apply immutable flag to {entry}")
            STATS["files"] += 1
            if not chattr("+i", entry):
                STATS["errors"] += 1


def main():
    lastbk_file   = SCRIPT_DIR / "lastbk.txt"
    lastlock_file = SCRIPT_DIR / "lastlock.txt"

    bktime   = lastbk_file.read_text().strip()   if lastbk_file.exists()   else ""
    locktime = lastlock_file.read_text().strip()  if lastlock_file.exists() else ""

    if bktime > locktime:
        try:
            recurse_folder(SCRIPT_DIR)
        except Exception as e:
            siem("target_lock", job_name(), files=STATS["files"], result="ERROR",
                 error=f"{type(e).__name__}: {e}")
            raise
        siem("target_lock", job_name(), files=STATS["files"],
             result="ERROR" if STATS["errors"] else "OK",
             **({"errors": STATS["errors"]} if STATS["errors"] else {}))

    # Rimuove flag immutable dai .txt prima di aggiornare lastlock.txt
    for txt in SCRIPT_DIR.glob("*.txt"):
        chattr("-i", txt)

    lastlock_file.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
