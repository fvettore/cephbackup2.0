#!/usr/bin/env python3
"""
bklock.py — applica ricorsivamente il flag IMMUTABLE (chattr +i)
a tutti i file del backup lato TARGET.
Esclude i file vmbackup.json (devono restare scrivibili).
Da mettere in cron.

© 2025 — GPLv3
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

SKIP_FILES = {"vmbackup.json"}


def chattr(flag: str, path: Path):
    subprocess.run(["chattr", flag, str(path)], check=False)


def recurse_folder(path: Path):
    for entry in sorted(path.iterdir()):
        if entry.is_dir():
            recurse_folder(entry)
        else:
            if entry.name in SKIP_FILES:
                print(f"skip immutable (excluded file): {entry}")
                continue
            print(f"apply immutable flag to {entry}")
            chattr("+i", entry)


def main():
    lastbk_file   = SCRIPT_DIR / "lastbk.txt"
    lastlock_file = SCRIPT_DIR / "lastlock.txt"

    bktime   = lastbk_file.read_text().strip()   if lastbk_file.exists()   else ""
    locktime = lastlock_file.read_text().strip()  if lastlock_file.exists() else ""

    if bktime > locktime:
        recurse_folder(SCRIPT_DIR)

    # Rimuove flag immutable dai .txt prima di aggiornare lastlock.txt
    for txt in SCRIPT_DIR.glob("*.txt"):
        chattr("-i", txt)

    lastlock_file.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
