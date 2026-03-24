#!/usr/bin/env python3
"""
bktrimsnap.py — elimina gli snapshot CEPH obsoleti per ogni immagine.
Per ogni job/VM mantiene solo gli ultimi `max-snaps` snapshot
il cui nome inizia con il prefisso del job (`snap-prefix`).
Versione databaseless di bktrimsnap.php.

Da mettere in cron daily.

© 2025 — GPLv3
"""

import configparser
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SKIP_DIRS  = {"LOGS", "VMDEF"}


# ── Config / jobs ─────────────────────────────────────────────────────────────

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return cfg


def load_jobs() -> list:
    with open(SCRIPT_DIR / "backupjobs.json") as f:
        return json.load(f)


# ── CEPH helpers ──────────────────────────────────────────────────────────────

def list_snapshots(pool: str, image: str) -> list:
    """Restituisce la lista degli snapshot di un'immagine CEPH (ordinata per id)."""
    cmd    = ["rbd", "snap", "ls", f"{pool}/{image}", "--format", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  WARNING: rbd snap ls fallito per {image}: {result.stderr.strip()}",
              file=sys.stderr)
        return []
    try:
        snaps = json.loads(result.stdout)
        # rbd restituisce gli snap ordinati per id crescente (dal più vecchio)
        return snaps
    except json.JSONDecodeError as e:
        print(f"  WARNING: JSON non valido da rbd snap ls: {e}", file=sys.stderr)
        return []


def remove_snapshot(pool: str, image: str, snap_name: str) -> bool:
    cmd = ["rbd", "snap", "rm", f"{pool}/{image}@{snap_name}"]
    print(f"  rbd snap rm {pool}/{image}@{snap_name}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ERROR: {r.stderr.strip()}", file=sys.stderr)
        return False
    return True


# ── Trim logic ────────────────────────────────────────────────────────────────

def trim_snapshots(pool: str, image: str, snap_prefix: str, max_snaps: int):
    """
    Elenca gli snapshot dell'immagine, filtra quelli con il prefisso del job
    e rimuove i più vecchi mantenendo solo gli ultimi max_snaps.
    """
    all_snaps  = list_snapshots(pool, image)
    bk_snaps   = [s['name'] for s in all_snaps if s['name'].startswith(snap_prefix)]

    to_remove  = len(bk_snaps) - max_snaps
    print(f"  snapshot totali con prefisso '{snap_prefix}': {len(bk_snaps)}  "
          f"max-snaps: {max_snaps}  da rimuovere: {max(to_remove, 0)}")

    if to_remove <= 0:
        print("  niente da rimuovere.")
        return

    for snap_name in bk_snaps[:to_remove]:
        remove_snapshot(pool, image, snap_name)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg        = load_config()
    pool       = cfg.get("ceph", "poolname", fallback="rbdpool01")
    jobs       = load_jobs()

    for job in jobs:
        if not job.get("enabled", 1):
            continue

        job_name    = job["name"]
        snap_prefix = job.get("snap-prefix", "BK")
        max_snaps   = job.get("max-snaps", 10)
        job_dir     = Path(job["path"]) / job_name

        if not job_dir.is_dir():
            print(f"[{job_name}] directory non trovata: {job_dir}")
            continue

        print(f"\n{'='*60}")
        print(f"Job: {job_name}  snap-prefix: {snap_prefix}  max-snaps: {max_snaps}")

        vm_dirs = sorted(
            d for d in job_dir.iterdir()
            if d.is_dir() and d.name not in SKIP_DIRS
        )

        for vm_dir in vm_dirs:
            vm_name = vm_dir.name
            images  = sorted(
                d.name for d in vm_dir.iterdir()
                if d.is_dir() and d.name not in SKIP_DIRS
            )
            if not images:
                continue

            print(f"\n  VM: {vm_name}")
            for image in images:
                print(f"    image: {image}")
                trim_snapshots(pool, image, snap_prefix, max_snaps)


if __name__ == "__main__":
    main()
