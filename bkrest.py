#!/usr/bin/env python3
"""
bkrest.py — ripristina un'immagine disco da un restore point.
Versione databaseless di bkrest.php.

USAGE: ./bkrest.py <JOB> <IMAGE> <RESTPOINT> <RESTORED-NAME>

  JOB          nome del job (da backupjobs.json)
  IMAGE        nome dell'immagine CEPH (es. PANTHERA-sda)
  RESTPOINT    punto di ripristino nel formato 000001-000003
  RESTORED-NAME nome della nuova immagine CEPH da creare

Esempio:
  ./bkrest.py NAS11day_01 PANTHERA-sda 000001-000003 PANTHERA-sda_rest

© 2025 — GPLv3
"""

import configparser
import getpass
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import bkcrypt

SCRIPT_DIR = Path(__file__).parent
RESTORE_LOG_DIR = SCRIPT_DIR / "RESTORELOGS"
STARTED  = datetime.now()
LOG_FILE = None


def lg(msg=""):
    """Stampa a video e scrive su RESTORELOGS con data/ora."""
    print(msg)
    if LOG_FILE is None:
        return
    try:
        RESTORE_LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(SCRIPT_DIR / "config.ini")
    return cfg


def load_jobs():
    with open(SCRIPT_DIR / "backupjobs.json") as f:
        return json.load(f)


def find_job(jobs, job_name):
    for job in jobs:
        if job["name"] == job_name:
            return job
    return None


def find_vm_for_image(job_dir, image_name):
    """
    Cerca in job_dir/$vm/$image_name la VM che contiene l'immagine.
    Restituisce il nome della VM o None.
    """
    SKIP = {"LOGS"}
    for vm_dir in job_dir.iterdir():
        if not vm_dir.is_dir() or vm_dir.name in SKIP:
            continue
        if (vm_dir / image_name).is_dir():
            return vm_dir.name
    return None


def run(cmd):
    """Esegue un comando mostrando output live. Restituisce il return code."""
    lg(" ".join(cmd))
    result = subprocess.run(cmd)
    lg(f"  rc={result.returncode}")
    return result.returncode


def resolve_key(job, enc_files):
    """
    Chiave per i file cifrati: quella registrata nel job se valida,
    altrimenti richiesta all'utente (3 tentativi). Restituisce (key_file, passphrase).
    """
    key_file = bkcrypt.key_file_for(job)
    if key_file:
        ok, msg = bkcrypt.check_key(enc_files[0], key_file=key_file)
        if ok:
            lg(f"Encryption key: file registrato nel job ({key_file}) — valida")
            return key_file, None
        lg(f"WARNING: la chiave registrata ({key_file}) non apre il backup: {msg}")
    else:
        lg("Encryption key: nessuna chiave registrata per il job")

    if not sys.stdin.isatty():
        lg("ERROR: backup cifrato e nessun terminale per chiedere la chiave")
        sys.exit(1)
    for attempt in range(1, 4):
        passphrase = getpass.getpass(f"Chiave di cifratura per il job {job['name']}: ")
        ok, msg = bkcrypt.check_key(enc_files[0], passphrase=passphrase)
        if ok:
            lg("Encryption key: inserita manualmente — valida")
            return None, passphrase
        lg(f"Chiave non valida (tentativo {attempt}/3): {msg}")
    lg("ERROR: chiave non valida, restore annullato")
    sys.exit(1)


def main():
    if len(sys.argv) < 5:
        print(f"USAGE: {sys.argv[0]} JOB IMAGE RESTPOINT RESTORED-NAME")
        sys.exit(1)

    job_name      = sys.argv[1]
    vm_image      = sys.argv[2]
    restore_point = sys.argv[3]
    restored_name = sys.argv[4]

    global LOG_FILE
    LOG_FILE = RESTORE_LOG_DIR / f"{STARTED:%Y%m%d_%H%M%S}_{restored_name}.log"

    # Valida formato RESTPOINT
    parts = restore_point.split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        lg(f"ERROR: RESTPOINT deve essere nel formato 000001-000003")
        sys.exit(1)

    instance = parts[0]   # es. 000001 (set full)
    point    = int(parts[1])  # es. 3 (fino all'inc 000003)

    cfg  = load_config()
    pool = cfg["ceph"]["poolname"]
    jobs = load_jobs()

    job = find_job(jobs, job_name)
    if not job:
        lg(f"ERROR: job '{job_name}' non trovato in backupjobs.json")
        sys.exit(1)

    job_dir = Path(job["path"]) / job_name

    # Trova la VM che contiene l'immagine
    vm_name = find_vm_for_image(job_dir, vm_image)
    if not vm_name:
        lg(f"ERROR: immagine '{vm_image}' non trovata nel job '{job_name}'")
        sys.exit(1)

    backup_path = job_dir / vm_name / vm_image / instance
    if not backup_path.is_dir():
        lg(f"ERROR: cartella backup non trovata: {backup_path}")
        sys.exit(1)

    lg(f"Job:          {job_name}")
    lg(f"VM:           {vm_name}")
    lg(f"Image:        {vm_image}")
    lg(f"Restore point: {restore_point}")
    lg(f"Backup path:  {backup_path}")
    lg(f"Destination:  {pool}/{restored_name}")
    lg()

    # Verifica che tutti i diff esistano prima di creare l'immagine
    diff_files = [bkcrypt.backup_file(backup_path, str(x).zfill(6)) for x in range(point + 1)]
    for diff_file in diff_files:
        if not diff_file.exists():
            lg(f"ERROR: file diff non trovato: {diff_file}")
            sys.exit(1)

    enc_files = [f for f in diff_files if bkcrypt.is_encrypted(f)]
    key_file = passphrase = None
    if enc_files:
        lg(f"Backup cifrato: {len(enc_files)}/{len(diff_files)} file")
        key_file, passphrase = resolve_key(job, enc_files)
        lg()

    # Crea immagine RBD vuota di destinazione
    lg(f"Creating empty image {restored_name} ...")
    rc = run(["rbd", "create", restored_name, "--size", "1024", "-p", pool])
    if rc != 0:
        lg(f"ERROR: rbd create fallito (rc={rc})")
        sys.exit(1)

    # Applica i diff dal full (000000) fino al punto richiesto
    for diff_file in diff_files:
        lg()
        lg(f"Importing diff {diff_file} ...")
        if diff_file in enc_files:
            target = f"{pool}/{restored_name}"
            lg(f"gpg --decrypt {diff_file} | rbd import-diff - {target}")
            rc, errlines = bkcrypt.import_encrypted(diff_file, target, key_file, passphrase)
            for line in errlines:
                lg(f"  {line}")
            lg(f"  rc={rc}")
        else:
            rc = run(["rbd", "import-diff", str(diff_file), f"{pool}/{restored_name}"])
        if rc != 0:
            lg(f"ERROR: rbd import-diff fallito su {diff_file} (rc={rc})")
            sys.exit(1)

    elapsed = int((datetime.now() - STARTED).total_seconds())
    lg()
    lg(f"Restore completato: {pool}/{restored_name}")
    lg(f"Tempo totale: {elapsed // 60}m {elapsed % 60:02d}s")
    lg(f"Log: {LOG_FILE}")


if __name__ == "__main__":
    main()
