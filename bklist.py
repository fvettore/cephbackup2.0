#!/usr/bin/env python3
"""
bklist.py — elenca i restore point disponibili per una VM.
Versione databaseless di bklist.php.

USAGE: ./bklist.py <VMNAME>

© 2025 — GPLv3
"""

import configparser
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return cfg


def load_jobs():
    with open(SCRIPT_DIR / "backupjobs.json") as f:
        return json.load(f)


def fmt_size(size):
    if size >= 1_000_000_000:
        return f"{size / 1e9:>8.2f} GB"
    elif size >= 1_000_000:
        return f"{size / 1e6:>8.2f} MB"
    else:
        return f"{size:>9}  B"


def list_restore_points(vm_name, jobs):
    found_any = False

    for job in jobs:
        job_name = job["name"]
        vm_dir   = Path(job["path"]) / job_name / vm_name

        if not vm_dir.is_dir():
            continue

        # Immagini = sottocartelle della vm_dir (escluse LOGS, VMDEFS)
        SKIP = {"LOGS", "VMDEF"}
        images = sorted(
            d for d in vm_dir.iterdir()
            if d.is_dir() and d.name not in SKIP
        )

        for image_dir in images:
            # Cartelle di primo livello (000001, 000002, ...)
            full_dirs = sorted(
                d for d in image_dir.iterdir()
                if d.is_dir() and d.name.isdigit() and len(d.name) == 6
            )
            if not full_dirs:
                continue

            print(f"\nVM: {vm_name}  image: {image_dir.name}  job: {job_name}")
            print(f"{'DATE':<20} {'TYPE':<6} {'RESTPOINT':<14} {'SIZE':>11}  {'BACKUP-JOB'}")
            print("-" * 70)

            for full_dir in full_dirs:
                indir = full_dir.name
                # Cartelle di secondo livello (000000=full, 000001+=inc)
                inc_entries = sorted(
                    e for e in full_dir.iterdir()
                    if e.is_file() and e.name.isdigit() and len(e.name) == 6
                )
                for entry in inc_entries:
                    incset  = entry.name
                    bktype  = "FULL" if incset == "000000" else "INC "
                    mtime   = datetime.fromtimestamp(entry.stat().st_mtime)
                    fdate   = mtime.strftime("%d-%m-%Y %H:%M:%S")
                    size    = entry.stat().st_size
                    restpoint = f"{indir}-{incset}"
                    print(f"{fdate}  {bktype}  {restpoint}  {fmt_size(size)}  {job_name}")

            found_any = True

    if not found_any:
        print(f"Nessun restore point trovato per la VM '{vm_name}'")


def main():
    if len(sys.argv) < 2:
        print(f"USAGE: {sys.argv[0]} VMNAME")
        sys.exit(1)

    vm_name = sys.argv[1]
    jobs    = load_jobs()
    list_restore_points(vm_name, jobs)


if __name__ == "__main__":
    main()
